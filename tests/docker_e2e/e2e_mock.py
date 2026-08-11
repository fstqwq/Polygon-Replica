"""HTTP-first deployed journey for the mock-Judgehost system E2E."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from domjudge_contract import require_approval
from runner import (
    _assert_active_internal_error_sanity,
    _assert_artifact_refs,
    _assert_late_diagnostics,
    _assert_mock_evidence,
    _assert_tasks,
    _connect,
    _latest_verification,
    _wait_for_mock_evidence,
    _wait_for_verification,
)


USERNAME = "e2e"
PROBLEM = "e2e/sample"
COMMIT_MESSAGE = "e2e-mock verified journey"


def _json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


FIXTURE_FILES = {
    "config/problem.json": _json_text(
        {
            "input_file": "stdin",
            "memory_limit_mb": 4,
            "mode": "pass-fail",
            "output_file": "stdout",
            "pass_limit": 1,
            "time_limit_ms": 2000,
        }
    ),
    "config/build.json": _json_text(
        {
            "accepted_solution_source": "solutions/main.cpp",
            "validator_source": "validators/validate.cpp",
            "checker_source": "",
            "generator_sources": ["generators/gen.py"],
        }
    ),
    "tests/spec.json": _json_text(
        {"tests": [{"id": "001", "kind": "gen", "sample": True}]}
    ),
    "tests/generator/001.in": "gen.py 7\n",
    "generators/gen.py": (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print(sys.argv[1])\n"
    ),
    "solutions/main.cpp": (
        "#include <iostream>\n"
        "int main() { long long value = 0; std::cin >> value; "
        "std::cout << value * value << '\\n'; }\n"
    ),
    "solutions/main.cpp.desc": "expected: accepted\n",
    "solutions/re.py": "raise RuntimeError('intentional E2E runtime error')\n",
    "solutions/re.py.desc": "expected: run_time_error\n",
    "solutions/ce.cpp": "this is intentionally not valid C++\n",
    "solutions/ce.cpp.desc": "expected: rejected\n",
    "validators/validate.cpp": (
        '#include "testlib.h"\n'
        "int main(int argc, char **argv) { registerValidation(argc, argv); "
        "inf.readLong(); inf.readEof(); }\n"
    ),
}


class _HiddenInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        name = attributes.get("name")
        if name:
            self.values[name] = attributes.get("value") or ""


def _hidden_inputs(response: httpx.Response) -> dict[str, str]:
    response.raise_for_status()
    parser = _HiddenInputParser()
    parser.feed(response.text)
    return parser.values


def _required_field(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "")
    if not value:
        raise RuntimeError(f"response omitted hidden form field {name!r}")
    return value


def _b64url_decode(value: str) -> bytes:
    payload = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(payload.encode("ascii"))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _password_verifier(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    ).hex()


def _password_envelope(
    client: httpx.Client,
    *,
    scope: str,
    purpose: str,
    username: str,
    csrf_token: str,
    verifier: str,
) -> dict[str, str]:
    response = client.get(
        "/auth/password-envelope",
        params={
            "scope": scope,
            "purpose": purpose,
            "username": username,
            "csrf_token": csrf_token,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("password envelope response is not an object")
    public_key = cast(
        rsa.RSAPublicKey,
        serialization.load_der_public_key(
            _b64url_decode(str(payload["public_key"]))
        ),
    )
    encrypted = public_key.encrypt(
        verifier.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "key_id": str(payload["key_id"]),
        "envelope_token": str(payload["envelope_token"]),
        "encrypted_verifier": _b64url_encode(encrypted),
    }


def _post(
    client: httpx.Client,
    path: str,
    data: dict[str, str],
) -> httpx.Response:
    response = client.post(
        path,
        data=data,
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    if response.status_code != 303:
        raise RuntimeError(
            f"POST {path} returned {response.status_code}: {response.text[:500]}"
        )
    return response


def _install_auth_cookie(
    client: httpx.Client,
    response: httpx.Response,
) -> None:
    cookies = list(response.cookies.items())
    if len(cookies) != 1:
        raise RuntimeError(
            f"authentication response did not set exactly one session cookie: {cookies!r}"
        )
    name, value = cookies[0]
    client.headers["Cookie"] = f"{name}={value}"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ["POLYGON_REPLICA_E2E_APP_ORIGIN"].rstrip("/"),
        follow_redirects=False,
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "polygon-replica-e2e-mock"},
    )


def _setup(client: httpx.Client) -> None:
    fields = _hidden_inputs(client.get("/setup"))
    csrf_token = _required_field(fields, "csrf_token")
    salt = _required_field(fields, "password_salt")
    iterations = int(_required_field(fields, "password_iters"))
    verifier = _password_verifier(
        os.environ["POLYGON_REPLICA_E2E_ADMIN_PASSWORD"],
        salt,
        iterations,
    )
    envelope = _password_envelope(
        client,
        scope="setup-password",
        purpose="setup",
        username=USERNAME,
        csrf_token=csrf_token,
        verifier=verifier,
    )
    response = _post(
        client,
        "/setup",
        {
            "username": USERNAME,
            "password": "",
            "password_confirm": "",
            "csrf_token": csrf_token,
            "password_salt": salt,
            "password_iters": str(iterations),
            "confirm_config": "1",
            "next": "/problems",
            **envelope,
        },
    )
    _install_auth_cookie(client, response)
    if response.headers.get("location") != "/problems":
        raise RuntimeError(f"setup redirected unexpectedly: {response.headers!r}")


def _login(client: httpx.Client) -> None:
    fields = _hidden_inputs(client.get("/login"))
    csrf_token = _required_field(fields, "csrf_token")
    metadata_response = client.get(
        "/auth/password-meta",
        params={"username": USERNAME, "csrf_token": csrf_token},
    )
    metadata_response.raise_for_status()
    metadata = metadata_response.json()
    if not isinstance(metadata, dict):
        raise RuntimeError("password metadata response is not an object")
    salt = str(metadata["salt"])
    iterations = int(metadata["iters"])
    verifier = _password_verifier(
        os.environ["POLYGON_REPLICA_E2E_ADMIN_PASSWORD"],
        salt,
        iterations,
    )
    envelope = _password_envelope(
        client,
        scope="login-password",
        purpose="login",
        username=USERNAME,
        csrf_token=csrf_token,
        verifier=verifier,
    )
    response = _post(
        client,
        "/login",
        {
            "username": USERNAME,
            "password": "",
            "csrf_token": csrf_token,
            "next": "/problems",
            **envelope,
        },
    )
    _install_auth_cookie(client, response)
    response = client.get("/problems")
    if response.status_code != 200:
        raise RuntimeError(
            f"login did not grant access to /problems: {response.status_code}"
        )


def _assert_fixture_shape() -> None:
    if len(FIXTURE_FILES) != len(set(FIXTURE_FILES)):
        raise RuntimeError("fixture contains duplicate paths")
    build = json.loads(FIXTURE_FILES["config/build.json"])
    required_sources = {
        str(build["accepted_solution_source"]),
        str(build["validator_source"]),
        *[str(path) for path in build["generator_sources"]],
    }
    missing = required_sources.difference(FIXTURE_FILES)
    if missing:
        raise RuntimeError(f"fixture build config references missing files: {missing!r}")
    if any("\r" in content for content in FIXTURE_FILES.values()):
        raise RuntimeError("fixture text must use LF line endings")


def prepare() -> None:
    require_approval()
    _assert_fixture_shape()
    with _client() as client:
        _setup(client)
        _post(
            client,
            "/admin/judgehosts/runtime",
            {
                "judgehost_enable": "1",
                "judgehost_api_username": "judgehost",
                "judgehost_api_token": os.environ[
                    "POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN"
                ],
            },
        )
        snapshot = client.get("/admin/judgehosts/snapshot")
        snapshot.raise_for_status()
        snapshot_payload = snapshot.json()
        if (
            not isinstance(snapshot_payload, dict)
            or snapshot_payload.get("enabled") is not True
            or snapshot_payload.get("auth_configured") is not True
        ):
            raise RuntimeError(f"Judgehost runtime was not enabled: {snapshot_payload!r}")

        response = _post(
            client,
            "/switch-workspace",
            {"problem": "sample", "page": "statement"},
        )
        if response.headers.get("location") != f"/problems/{PROBLEM}/statement":
            raise RuntimeError(
                f"problem creation redirected unexpectedly: {response.headers!r}"
            )
        for path, content in FIXTURE_FILES.items():
            _post(
                client,
                f"/problems/{PROBLEM}/files/save",
                {"path": path, "content": content, "dir": str(Path(path).parent)},
            )
            saved = client.get(
                f"/problems/{PROBLEM}/files/download",
                params={"path": path},
            )
            if saved.status_code != 200 or saved.content != content.encode("utf-8"):
                raise RuntimeError(f"HTTP save did not round-trip fixture file {path!r}")
    print("e2e-mock prepared a fresh deployment and authored the fixture over HTTP")


def _journey_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            p.id AS problem_id,
            p.repo_name,
            w.id AS workspace_id,
            w.path AS workspace_path,
            w.head_commit,
            w.dirty
        FROM problems p
        JOIN workspaces w ON w.problem_id=p.id
        JOIN users u ON u.id=w.user_id
        WHERE p.slug=? AND u.username=?
        """,
        [PROBLEM, USERNAME],
    ).fetchone()
    if row is None:
        raise RuntimeError("HTTP authoring did not persist the problem workspace")
    return row


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _assert_mock_payload_hashes(state: dict[str, object]) -> None:
    events = state.get("events")
    if not isinstance(events, list):
        raise RuntimeError("mock evidence has no event list")
    expected_sources = {
        Path(path).name: _sha256(content.encode("utf-8"))
        for path, content in FIXTURE_FILES.items()
        if path in {
            "generators/gen.py",
            "solutions/main.cpp",
            "solutions/re.py",
            "solutions/ce.cpp",
        }
    }
    for source, expected_hash in expected_sources.items():
        matching = [
            event
            for event in events
            if isinstance(event, dict) and event.get("source") == source
        ]
        if not matching:
            raise RuntimeError(f"mock did not receive source payload {source!r}")
        observed = {
            str(source_hashes.get(source) or "")
            for event in matching
            if isinstance(source_hashes := event.get("source_sha256s"), dict)
        }
        if observed != {expected_hash}:
            raise RuntimeError(
                f"mock received unexpected bytes for {source!r}: {observed!r}"
            )

    expected_testcases = {
        "gen.py": {
            "input": _sha256(b'"$SUBMISSION_BIN" 7\n'),
            "output": _sha256(b""),
        },
        "main.cpp": {
            "input": _sha256(b"7\n"),
            "output": _sha256(b""),
        },
        "re.py": {
            "input": _sha256(b"7\n"),
            "output": _sha256(b"49\n"),
        },
    }
    for source, expected_hashes in expected_testcases.items():
        matching = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("kind") == "completed"
            and event.get("source") == source
        ]
        if not any(
            isinstance(testcase_hashes := event.get("testcase_sha256s"), dict)
            and {
                "input": testcase_hashes.get("input"),
                "output": testcase_hashes.get("output"),
            }
            == expected_hashes
            for event in matching
        ):
            raise RuntimeError(
                f"mock received unexpected testcase bytes for {source!r}"
            )

    expected_outputs = {
        "gen.py": _sha256(b"7\n"),
        "main.cpp": _sha256(b"49\n"),
    }
    for source, expected_hash in expected_outputs.items():
        if not any(
            isinstance(event, dict)
            and event.get("kind") == "completed"
            and event.get("source") == source
            and event.get("output_sha256") == expected_hash
            for event in events
        ):
            raise RuntimeError(f"mock output digest is wrong for {source!r}")


def _assert_public_artifacts(
    client: httpx.Client,
    verification_id: str,
) -> None:
    expected = {
        "tests/001.in": b"7\n",
        "ans/001.ans": b"49\n",
    }
    for relative_path, content in expected.items():
        response = client.get(
            f"/problems/{PROBLEM}/artifacts/{verification_id}/{relative_path}"
        )
        if response.status_code != 200 or response.content != content:
            raise RuntimeError(
                f"public artifact {relative_path!r} was unavailable or incorrect"
            )


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


def _resolved_child(root: Path, child: Path) -> Path:
    resolved_root = root.resolve()
    resolved_child = child.resolve()
    if resolved_child == resolved_root or resolved_root not in resolved_child.parents:
        raise RuntimeError(f"path escaped expected E2E root: {resolved_child}")
    return resolved_child


def _assert_commit(connection: sqlite3.Connection) -> str:
    row = _journey_row(connection)
    workspace = _resolved_child(
        Path(os.environ["POLYGON_REPLICA_E2E_WORKSPACE_ROOT"]),
        Path(str(row["workspace_path"])),
    )
    bare = _resolved_child(
        Path(os.environ["POLYGON_REPLICA_E2E_BARE_ROOT"]),
        Path(os.environ["POLYGON_REPLICA_E2E_BARE_ROOT"])
        / str(row["repo_name"]),
    )
    workspace_head = _git("-C", str(workspace), "rev-parse", "HEAD")
    bare_head = _git(
        "--git-dir",
        str(bare),
        "rev-parse",
        "refs/heads/main",
    )
    if workspace_head != bare_head:
        raise RuntimeError("workspace HEAD and published bare main differ")
    if _git("-C", str(workspace), "status", "--porcelain"):
        raise RuntimeError("workspace is dirty after revision commit")
    subject = _git("-C", str(workspace), "show", "-s", "--format=%s", "HEAD")
    if subject != COMMIT_MESSAGE:
        raise RuntimeError(f"unexpected commit subject: {subject!r}")
    if _git("-C", str(workspace), "rev-list", "--count", "HEAD") != "1":
        raise RuntimeError("e2e-mock initial publication is not a root commit")
    commit_line = _git(
        "-C",
        str(workspace),
        "rev-list",
        "--parents",
        "-n",
        "1",
        "HEAD",
    )
    if len(commit_line.split()) != 1:
        raise RuntimeError("e2e-mock initial publication unexpectedly has a parent")
    committed_source = _git(
        "-C",
        str(workspace),
        "show",
        "HEAD:solutions/main.cpp",
    )
    if committed_source + "\n" != FIXTURE_FILES["solutions/main.cpp"]:
        raise RuntimeError("published commit does not contain the verified solution")
    if str(row["head_commit"] or "") != workspace_head or int(row["dirty"]) != 0:
        raise RuntimeError(
            "SQLite workspace status does not match the published clean commit"
        )

    actions = {
        str(audit_row["action"])
        for audit_row in connection.execute(
            "SELECT action FROM audit_log ORDER BY id"
        ).fetchall()
    }
    required_actions = {
        "system.setup",
        "system_config.update_judgehost_runtime_controls",
        "verification.start",
        "revision.commit",
    }
    missing_actions = required_actions.difference(actions)
    if missing_actions:
        raise RuntimeError(f"journey audit trail is incomplete: {missing_actions!r}")
    commit_audit = connection.execute(
        """
        SELECT details_json
        FROM audit_log
        WHERE action='revision.commit'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if commit_audit is None:
        raise RuntimeError("journey audit omitted revision.commit details")
    commit_details = json.loads(str(commit_audit["details_json"]))
    if not isinstance(commit_details, dict) or commit_details != {
        "message": COMMIT_MESSAGE,
        "head": workspace_head,
    }:
        raise RuntimeError(f"revision.commit audit details are wrong: {commit_details!r}")
    save_count = connection.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='files.save'"
    ).fetchone()[0]
    if int(save_count) != len(FIXTURE_FILES):
        raise RuntimeError(
            f"journey audit recorded {save_count} saves, expected {len(FIXTURE_FILES)}"
        )
    return workspace_head


def verify_and_commit() -> None:
    approval = require_approval()
    with _client() as client:
        _login(client)
        with _connect() as connection:
            context = _journey_row(connection)
            problem_id = int(context["problem_id"])
            workspace_id = int(context["workspace_id"])
            previous = _latest_verification(
                connection,
                problem_id=problem_id,
                workspace_id=workspace_id,
            )
            previous_id = "" if previous is None else str(previous["id"])
            _post(
                client,
                f"/problems/{PROBLEM}/verification/start",
                {"page": "tests"},
            )
            verification = _wait_for_verification(
                connection,
                problem_id=problem_id,
                workspace_id=workspace_id,
                previous_id=previous_id,
            )
            if str(verification["kind"]) != "all":
                raise RuntimeError(
                    f"verification did not cover the full test set: {dict(verification)!r}"
                )
            if str(verification["status"]) != "ok":
                raise RuntimeError(f"verification failed: {dict(verification)!r}")
            if str(verification["fail_reason"] or ""):
                raise RuntimeError(
                    f"successful verification retained a failure: {dict(verification)!r}"
                )
            if str(verification["sanity_status"] or "") != "failed":
                raise RuntimeError(
                    "mock active-internal-error sanity case was not retained"
                )
            verification_id = str(verification["id"])
            _assert_tasks(connection, verification_id)
            _assert_artifact_refs(connection, verification_id)
            _assert_active_internal_error_sanity(connection, verification_id)
            _assert_public_artifacts(client, verification_id)

        mock_state = _wait_for_mock_evidence()
        _assert_mock_evidence(
            mock_state,
            approved_source_sha256s=approval["source_sha256s"],
        )
        _assert_mock_payload_hashes(mock_state)
        with _connect() as connection:
            _assert_late_diagnostics(connection, verification_id)

        _post(
            client,
            f"/problems/{PROBLEM}/revision/commit",
            {"message": COMMIT_MESSAGE},
        )
        refreshed = client.get(f"/problems/{PROBLEM}/workspace")
        if refreshed.status_code != 200:
            raise RuntimeError(
                f"workspace status refresh returned {refreshed.status_code}"
            )

    with _connect() as connection:
        head = _assert_commit(connection)
    print(
        "e2e-mock completed deployment, authoring, verification, and commit "
        f"verification={verification_id} head={head}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify-commit"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
        return
    verify_and_commit()


if __name__ == "__main__":
    main()
