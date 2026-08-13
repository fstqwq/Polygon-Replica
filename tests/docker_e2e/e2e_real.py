"""HTTP-first deployed journey backed by an official DOMjudge Judgehost."""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from e2e_real_contest import (
    CONTEST,
    assert_contest_pdf,
    start_contest_pdf,
    wait_for_contest_job,
)
from e2e_real_roles import exercise_role_pages_and_collaboration
from runner import (
    _assert_artifact_refs,
    _assert_preview_sample_materialization,
    _assert_tasks,
    _connect,
    _latest_verification,
    _wait_for_verification,
)


USERNAME = "e2e"
PROBLEM = "e2e/sample"
COMMIT_MESSAGE = "e2e-real verified journey"
AGENT_ROOT = Path(os.environ["POLYGON_REPLICA_E2E_STATE_DIR"]) / "agent-cli"
AGENT_STATE = AGENT_ROOT / "state.json"
AGENT_REPO = AGENT_ROOT / PROBLEM
AGENT_TEMP = AGENT_ROOT / "temp"
AGENT_SPECIAL_VERDICTS = {
    "solutions/wa.cpp": "WA",
    "solutions/ce.cpp": "CE",
}


def _json_text(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


BASE_FIXTURE_FILES = {
    "config/problem.json": _json_text(
        {
            "memory_limit_mb": 64,
            "mode": "pass-fail",
            "pass_limit": 1,
            "time_limit_ms": 2000,
        }
    ),
    "config/build.json": _json_text(
        {
            "accepted_solution_source": "solutions/main.cpp",
            "validator_source": "validators/validate.cpp",
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
    "solutions/wa.cpp": (
        "#include <iostream>\n"
        "int main() { std::cout << 0 << '\\n'; }\n"
    ),
    "solutions/wa.cpp.desc": "expected: wrong_answer\n",
    "solutions/ce.cpp": "this is intentionally not valid C++\n",
    "solutions/ce.cpp.desc": "expected: rejected\n",
    "validators/validate.cpp": (
        '#include "testlib.h"\n'
        "int main(int argc, char **argv) { registerValidation(argc, argv); "
        "inf.readLong(); inf.readEoln(); inf.readEof(); }\n"
    ),
}


def _fixture_files() -> dict[str, str]:
    skills_root = Path(os.environ["POLYGON_REPLICA_E2E_SKILLS_ROOT"])
    template_root = skills_root / "polygon-init" / "templates"
    files = dict(BASE_FIXTURE_FILES)
    files.update(
        {
            "statement/statements.ftl": (template_root / "statements.ftl").read_text(
                encoding="utf-8"
            ),
            "statement/problem.tex": (template_root / "problem.tex").read_text(
                encoding="utf-8"
            ),
            "statement/olymp.sty": (template_root / "olymp.sty").read_text(
                encoding="utf-8"
            ),
            "statement-sections/english/name.tex": "E2E Square\n",
            "statement-sections/english/legend.tex": "Square the input integer.\n",
            "statement-sections/english/input.tex": "One integer.\n",
            "statement-sections/english/output.tex": "Its square.\n",
            "statement-sections/english/notes.tex": "\n",
        }
    )
    return files


FIXTURE_FILES = _fixture_files()


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
    *,
    timeout_sec: float = 30.0,
) -> httpx.Response:
    response = client.post(
        path,
        data=data,
        headers={"Origin": str(client.base_url).rstrip("/")},
        timeout=timeout_sec,
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
        headers={"User-Agent": "polygon-replica-e2e-real"},
    )


def _agent_cli(*args: str, expect_ok: bool = True) -> dict[str, object]:
    cli = (
        Path(os.environ["POLYGON_REPLICA_E2E_SKILLS_ROOT"])
        / "polygon-agent-cli"
        / "scripts"
        / "polygon_agent.py"
    )
    command = [
        "python3",
        str(cli),
        *args,
        "--state-file",
        str(AGENT_STATE),
    ]
    completed = subprocess.run(
        command,
        cwd=AGENT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"Agent CLI did not emit exactly one JSON line: {command!r}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Agent CLI emitted invalid JSON: {lines[0]!r}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise RuntimeError(f"Agent CLI emitted an invalid envelope: {payload!r}")
    if expect_ok:
        if completed.returncode != 0 or payload["ok"] is not True:
            raise RuntimeError(
                f"Agent CLI command failed: {command!r}\n"
                f"payload={payload!r}\nstderr={completed.stderr!r}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Agent CLI success omitted result: {payload!r}")
        print(f"agent-cli {args[0]} ok")
        return cast(dict[str, object], result)
    if completed.returncode == 0 or payload["ok"] is not False:
        raise RuntimeError(
            f"Agent CLI command unexpectedly succeeded: {command!r}"
        )
    error = payload.get("error")
    if not isinstance(error, dict):
        raise RuntimeError(f"Agent CLI failure omitted error: {payload!r}")
    print(f"agent-cli {args[0]} failed as expected")
    return cast(dict[str, object], error)


def _agent_approve(
    client: httpx.Client,
    approve_url: str,
    *,
    scope: str,
) -> None:
    parsed = urlparse(approve_url)
    review = client.get(parsed.path)
    if review.status_code != 200:
        raise RuntimeError(
            f"Agent approval page returned {review.status_code}: {review.text[:500]}"
        )
    response = _post(
        client,
        parsed.path,
        {"decision": "approve", "scope": scope, "ttl": "86400"},
    )
    if response.headers.get("location") != "/agent/sessions":
        raise RuntimeError(
            f"Agent approval redirected unexpectedly: {response.headers!r}"
        )


def _agent_register_url(client: httpx.Client) -> str:
    response = client.post(
        "/agent/connect",
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"Agent connect failed: {payload!r}")
    return str(payload.get("register_url") or "")


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


def _register_user(username: str, password: str) -> httpx.Client:
    client = _client()
    try:
        fields = _hidden_inputs(client.get("/register"))
        csrf_token = _required_field(fields, "csrf_token")
        salt = _required_field(fields, "password_salt")
        iterations = int(_required_field(fields, "password_iters"))
        verifier = _password_verifier(password, salt, iterations)
        envelope = _password_envelope(
            client,
            scope="register-password",
            purpose="register",
            username=username,
            csrf_token=csrf_token,
            verifier=verifier,
        )
        response = _post(
            client,
            "/register",
            {
                "username": username,
                "email": f"{username}@gmail.com",
                "password": "",
                "password_confirm": "",
                "csrf_token": csrf_token,
                "password_salt": salt,
                "password_iters": str(iterations),
                "next": "/problems",
                "terms_accepted": "yes",
                **envelope,
            },
        )
        _install_auth_cookie(client, response)
        if response.headers.get("location") != "/problems":
            raise RuntimeError(
                f"registration for {username!r} redirected unexpectedly: "
                f"{response.headers!r}"
            )
        session_check = client.get("/problems")
        if session_check.status_code != 200:
            raise RuntimeError(
                f"registration for {username!r} did not establish a session: "
                f"{session_check.status_code}"
            )
        return client
    except Exception:
        client.close()
        raise


def _assert_fixture_shape() -> None:
    if len(FIXTURE_FILES) != len(set(FIXTURE_FILES)):
        raise RuntimeError("fixture contains duplicate paths")
    build = json.loads(FIXTURE_FILES["config/build.json"])
    required_sources = {
        str(build["accepted_solution_source"]),
        str(build["validator_source"]),
        "generators/gen.py",
    }
    missing = required_sources.difference(FIXTURE_FILES)
    if missing:
        raise RuntimeError(f"fixture build config references missing files: {missing!r}")
    if any("\r" in content for content in FIXTURE_FILES.values()):
        raise RuntimeError("fixture text must use LF line endings")


def _exercise_legacy_build_normalization(client: httpx.Client) -> None:
    canonical = cast(
        dict[str, object],
        json.loads(FIXTURE_FILES["config/build.json"]),
    )
    legacy = {
        **canonical,
        "checker_args": ["--removed"],
        "compile_jobs": 0,
        "run_timeout_sec": 30,
    }
    upload_source = AGENT_TEMP / "legacy-build.json"
    upload_source.write_text(_json_text(legacy), encoding="utf-8", newline="\n")
    _agent_cli(
        "upload",
        "--problem",
        PROBLEM,
        "--workspace-path",
        "config/build.json",
        "--local-file",
        str(upload_source),
    )
    before = _agent_cli(
        "read-file",
        "--problem",
        PROBLEM,
        "--path",
        "config/build.json",
    )
    if json.loads(str(before.get("content") or "")) != legacy:
        raise RuntimeError("Agent did not install the legacy build fixture")

    page = client.get(f"/problems/{PROBLEM}/workspace")
    if page.status_code != 200:
        raise RuntimeError(
            "legacy build source made the authoring page unavailable: "
            f"{page.status_code} {page.text[:500]}"
        )
    if "obsolete fields were removed" not in page.text:
        raise RuntimeError("Review and Publish omitted the legacy normalization warning")

    after = _agent_cli(
        "read-file",
        "--problem",
        PROBLEM,
        "--path",
        "config/build.json",
    )
    if json.loads(str(after.get("content") or "")) != canonical:
        raise RuntimeError("legacy build normalization changed current selections")


def prepare() -> None:
    _assert_fixture_shape()
    AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    AGENT_TEMP.mkdir(parents=True, exist_ok=True)
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

        register_url = _agent_register_url(client)
        initialized = _agent_cli(
            "init",
            "--register-url",
            register_url,
            "--agent-name",
            "Polygon Replica E2E",
            "--desktop-id",
            "polygon-replica-ci",
            "--init-ts",
            "2026-08-12T00:00:00Z",
        )
        if initialized.get("user") != USERNAME:
            raise RuntimeError(f"Agent initialized for wrong user: {initialized!r}")
        status = _agent_cli("status")
        if status.get("user") != USERNAME or status.get("authorized_problems") != []:
            raise RuntimeError(f"fresh Agent status is wrong: {status!r}")

        created = _agent_cli("create", "--problem", PROBLEM)
        if created.get("problem") != PROBLEM:
            raise RuntimeError(f"Agent created the wrong problem: {created!r}")
        duplicate = _agent_cli(
            "create",
            "--problem",
            PROBLEM,
            expect_ok=False,
        )
        if int(duplicate.get("http_status") or 0) != 409:
            raise RuntimeError(f"duplicate Agent create was not rejected: {duplicate!r}")

        access = _agent_cli("connect", "--problem", PROBLEM)
        request_id = str(access.get("request_id") or "")
        approve_url = str(access.get("approve_url") or "")
        if not request_id or not approve_url:
            raise RuntimeError(f"Agent connect omitted approval data: {access!r}")
        pending = _agent_cli("poll", "--request-id", request_id)
        if pending.get("status") != "pending":
            raise RuntimeError(f"Agent access was not pending: {pending!r}")
        _agent_approve(client, approve_url, scope="commit")
        approved = _agent_cli(
            "poll",
            "--request-id",
            request_id,
            "--wait",
            "--interval-sec",
            "0.1",
            "--timeout-sec",
            "10",
        )
        if approved.get("status") != "approved" or approved.get("token_saved") is not True:
            raise RuntimeError(f"Agent token was not saved: {approved!r}")
        authorized = _agent_cli("status")
        authorized_problems = cast(
            list[dict[str, object]],
            authorized.get("authorized_problems") or [],
        )
        if authorized_problems != [
            {
                "problem": PROBLEM,
                "scope": "commit",
                "expires_at": approved.get("expires_at"),
            }
        ]:
            raise RuntimeError(
                f"Agent status omitted approved problem access: {authorized!r}"
            )

        cloned = _agent_cli(
            "clone",
            "--problem",
            PROBLEM,
            "--target-dir",
            str(AGENT_REPO),
        )
        if cloned.get("created_repo") is not True or not (AGENT_REPO / ".git").is_dir():
            raise RuntimeError(f"Agent clone did not create a local Git repo: {cloned!r}")
        workspace_status = _agent_cli("workspace-status", "--problem", PROBLEM)
        if workspace_status.get("problem") != PROBLEM:
            raise RuntimeError(f"Agent workspace status is wrong: {workspace_status!r}")
        listed = _agent_cli("list-files", "--problem", PROBLEM, "--path", "config")
        listed_paths = {
            str(entry.get("path") or "")
            for entry in cast(list[dict[str, object]], listed.get("entries") or [])
        }
        if "config/problem.json" not in listed_paths:
            raise RuntimeError(f"Agent list-files omitted problem.json: {listed!r}")
        problem_file = _agent_cli(
            "read-file",
            "--problem",
            PROBLEM,
            "--path",
            "config/problem.json",
        )
        if '"mode": "pass-fail"' not in str(problem_file.get("content") or ""):
            raise RuntimeError(f"Agent read-file returned wrong content: {problem_file!r}")
        saved_problem = AGENT_TEMP / "problem.json"
        _agent_cli(
            "read-file",
            "--problem",
            PROBLEM,
            "--path",
            "config/problem.json",
            "--save-to",
            str(saved_problem),
        )
        if not saved_problem.is_file():
            raise RuntimeError("Agent read-file --save-to did not create a file")

        upload_source = AGENT_TEMP / "pull.txt"
        upload_source.write_text("pulled through Agent CLI\n", encoding="utf-8")
        _agent_cli(
            "upload",
            "--problem",
            PROBLEM,
            "--workspace-path",
            "attachments/pull.txt",
            "--local-file",
            str(upload_source),
        )
        pulled = _agent_cli(
            "pull",
            "--problem",
            PROBLEM,
            "--target-dir",
            str(AGENT_REPO),
        )
        if pulled.get("changed") is not True or not (AGENT_REPO / "attachments/pull.txt").is_file():
            raise RuntimeError(f"Agent pull did not synchronize upload: {pulled!r}")
        _agent_cli(
            "delete",
            "--problem",
            PROBLEM,
            "--workspace-path",
            "attachments/pull.txt",
        )
        deleted_read = _agent_cli(
            "read-file",
            "--problem",
            PROBLEM,
            "--path",
            "attachments/pull.txt",
            expect_ok=False,
        )
        if int(deleted_read.get("http_status") or 0) != 404:
            raise RuntimeError(f"Agent delete did not remove the file: {deleted_read!r}")
        _agent_cli(
            "pull",
            "--problem",
            PROBLEM,
            "--target-dir",
            str(AGENT_REPO),
        )
        if (AGENT_REPO / "attachments/pull.txt").exists():
            raise RuntimeError("Agent pull retained a remotely deleted file")

        for path, content in FIXTURE_FILES.items():
            target = AGENT_REPO / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        pushed = _agent_cli(
            "push",
            "--problem",
            PROBLEM,
            "--target-dir",
            str(AGENT_REPO),
        )
        if pushed.get("applied") is not True or pushed.get("changed") is not True:
            raise RuntimeError(f"Agent push did not apply fixture: {pushed!r}")
        _exercise_legacy_build_normalization(client)
        _agent_cli(
            "pull",
            "--problem",
            PROBLEM,
            "--target-dir",
            str(AGENT_REPO),
        )
    print(
        "e2e-real prepared a fresh deployment with every authoring write through "
        f"Polygon Agent CLI from Skills {os.environ['POLYGON_REPLICA_E2E_SKILLS_COMMIT']}"
    )


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


def _assert_accepted_solution(
    connection: sqlite3.Connection,
    verification_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT final_status,result_json
        FROM verification_tasks
        WHERE verification_id=? AND task_kind='main-correct'
          AND source_path='solutions/main.cpp' AND test_name='001.in'
        """,
        [verification_id],
    ).fetchone()
    if row is None or str(row["final_status"]) != "done":
        raise RuntimeError(f"accepted solution task did not complete: {row!r}")
    result = json.loads(str(row["result_json"]))
    outcome = result.get("outcome") if isinstance(result, dict) else None
    verdict = outcome.get("verdict") if isinstance(outcome, dict) else None
    if verdict != "OK":
        raise RuntimeError(f"accepted solution verdict is not OK: {verdict!r}")


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


def _git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


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
        raise RuntimeError("e2e-real initial publication is not a root commit")
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
        raise RuntimeError("e2e-real initial publication unexpectedly has a parent")
    for relative_path, expected_text in FIXTURE_FILES.items():
        committed = _git_bytes(
            "--git-dir",
            str(bare),
            "show",
            f"{workspace_head}:{relative_path}",
        )
        if committed != expected_text.encode("utf-8"):
            raise RuntimeError(
                f"published commit contains unexpected bytes for {relative_path!r}"
            )
    if str(row["head_commit"] or "") != workspace_head or int(row["dirty"]) != 0:
        raise RuntimeError(
            "SQLite workspace status does not match the published clean commit"
        )

    return workspace_head


def _assert_agent_verification_detail(verification_id: str) -> None:
    detail = _agent_cli(
        "verify-detail",
        "--problem",
        PROBLEM,
        "--verification-id",
        verification_id,
    )
    detail_text = str(detail.get("detail_text") or "")
    if (
        f"verification: {verification_id}" not in detail_text
        or "status: ok" not in detail_text
        or "solutions/main.cpp" not in detail_text
    ):
        raise RuntimeError(f"Agent verification detail is incomplete: {detail_text!r}")

    saved_detail = AGENT_TEMP / f"{verification_id}.yaml"
    saved = _agent_cli(
        "verify-detail",
        "--problem",
        PROBLEM,
        "--verification-id",
        verification_id,
        "--save-to",
        str(saved_detail),
    )
    if saved.get("saved_to") != str(saved_detail) or not saved_detail.is_file():
        raise RuntimeError(f"Agent verification detail was not saved: {saved!r}")
    if saved_detail.read_text(encoding="utf-8") != detail_text:
        raise RuntimeError("saved Agent verification detail differs from inline output")

    test_detail = _agent_cli(
        "verify-detail",
        "--problem",
        PROBLEM,
        "--verification-id",
        verification_id,
        "--test-name",
        "001.in",
    )
    test_text = str(test_detail.get("detail_text") or "")
    if "test: 001.in" not in test_text or "solutions/main.cpp" not in test_text:
        raise RuntimeError(f"Agent testcase detail is incomplete: {test_text!r}")

    cell_detail = _agent_cli(
        "verify-detail",
        "--problem",
        PROBLEM,
        "--verification-id",
        verification_id,
        "--test-name",
        "001.in",
        "--source",
        "solutions/main.cpp",
    )
    cell_text = str(cell_detail.get("detail_text") or "")
    if (
        "test: 001.in" not in cell_text
        or "cell:" not in cell_text
        or "source: solutions/main.cpp" not in cell_text
    ):
        raise RuntimeError(f"Agent result-cell detail is incomplete: {cell_text!r}")


def _agent_export(export_type: str) -> tuple[str, Path]:
    started = _agent_cli(
        "export-start",
        "--problem",
        PROBLEM,
        "--export-type",
        export_type,
    )
    job_id = str(started.get("job_id") or "")
    if not job_id or started.get("status") != "queued":
        raise RuntimeError(f"Agent {export_type} export did not start: {started!r}")
    waited = _agent_cli(
        "export-wait",
        "--problem",
        PROBLEM,
        "--job-id",
        job_id,
        "--interval-sec",
        "0.1",
        "--timeout-sec",
        "300",
    )
    if waited.get("job_id") != job_id or waited.get("status") != "succeeded":
        raise RuntimeError(f"Agent {export_type} export failed: {waited!r}")
    filename = str(waited.get("filename") or "")
    if Path(filename).name != filename or not filename.endswith(".zip"):
        raise RuntimeError(f"Agent {export_type} export filename is wrong: {waited!r}")

    output = AGENT_TEMP / f"{job_id}-{filename}"
    downloaded = _agent_cli(
        "export-download",
        "--problem",
        PROBLEM,
        "--job-id",
        job_id,
        "--output",
        str(output),
    )
    if (
        downloaded.get("job_id") != job_id
        or downloaded.get("output") != str(output)
        or int(downloaded.get("bytes_written") or 0) != output.stat().st_size
    ):
        raise RuntimeError(f"Agent {export_type} download is inconsistent: {downloaded!r}")
    with zipfile.ZipFile(output) as archive:
        members = set(archive.namelist())
        required_member = "config/problem.json" if export_type == "native" else "problem.yaml"
        if required_member not in members:
            raise RuntimeError(
                f"Agent {export_type} archive omitted {required_member}: {sorted(members)!r}"
            )
        if archive.testzip() is not None:
            raise RuntimeError(f"Agent {export_type} archive contains a corrupt member")
    return job_id, output


def _run_statement_preview(
    client: httpx.Client,
    connection: sqlite3.Connection,
    *,
    problem_id: int,
    workspace_id: int,
    previous_id: str,
) -> str:
    response = _post(
        client,
        f"/problems/{PROBLEM}/preview/run",
        {"page": "statement", "language": "english"},
        timeout_sec=300.0,
    )
    location = response.headers.get("location", "")
    preview_ids = parse_qs(urlparse(location).query).get("preview_id", [])
    if len(preview_ids) != 1 or not preview_ids[0]:
        raise RuntimeError(
            "statement preview did not start a compile: "
            f"location={location!r} set-cookie={response.headers.get('set-cookie', '')!r}"
        )
    verification = _wait_for_verification(
        connection,
        problem_id=problem_id,
        workspace_id=workspace_id,
        previous_id=previous_id,
    )
    if (
        str(verification["kind"]) != "sample"
        or str(verification["status"]) != "ok"
        or str(verification["fail_reason"] or "")
    ):
        raise RuntimeError(
            f"statement preview sample verification failed: {dict(verification)!r}"
        )
    verification_id = str(verification["id"])
    _assert_preview_sample_materialization(
        connection,
        problem_id=problem_id,
        workspace_id=workspace_id,
        verification_id=verification_id,
    )
    return verification_id


def _real_judgehost(client: httpx.Client) -> dict[str, object] | None:
    expected_hostname = os.environ["POLYGON_REPLICA_E2E_JUDGEHOST_HOSTNAME"]
    response = client.get("/admin/judgehosts/snapshot")
    response.raise_for_status()
    payload = response.json()
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, list):
        raise RuntimeError(f"Judgehost snapshot omitted hosts: {payload!r}")
    matches = [
        host
        for host in hosts
        if isinstance(host, dict) and host.get("hostname") == expected_hostname
    ]
    if not matches:
        return None
    if len(matches) != 1 or len(hosts) != 1:
        raise RuntimeError(f"Judgehost stack is not isolated: {hosts!r}")
    return cast(dict[str, object], matches[0])


def _wait_for_real_judgehost(client: httpx.Client, timeout_sec: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        last = _real_judgehost(client)
        if last is not None and last.get("online") is True:
            return
        time.sleep(0.5)
    raise RuntimeError(f"real Judgehost did not register online: {last!r}")


def _assert_real_judgehost_executed(client: httpx.Client) -> None:
    host = _real_judgehost(client)
    if host is None:
        raise RuntimeError("real Judgehost disappeared after verification")
    if host.get("online") is not True or int(host.get("judged_case_count") or 0) < 1:
        raise RuntimeError(f"real Judgehost did not execute a case: {host!r}")


def verify_deployment() -> None:
    product_tail = os.environ.get("POLYGON_REPLICA_E2E_PRODUCT_TAIL") == "1"
    with _client() as client:
        _login(client)
        _wait_for_real_judgehost(client)
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
            sample_verification_id = ""
            if product_tail:
                sample_verification_id = _run_statement_preview(
                    client,
                    connection,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    previous_id=previous_id,
                )
                previous_id = sample_verification_id
            started = _agent_cli(
                "verify-start",
                "--problem",
                PROBLEM,
            )
            verification_id = str(started.get("verification_id") or "")
            if not verification_id or started.get("status") != "queued":
                raise RuntimeError(f"Agent verification did not start: {started!r}")
            waited = _agent_cli(
                "verify-wait",
                "--problem",
                PROBLEM,
                "--verification-id",
                verification_id,
                "--interval-sec",
                "0.1",
                "--timeout-sec",
                "300",
            )
            if waited != {"verification_id": verification_id, "status": "ok"}:
                raise RuntimeError(f"Agent verification failed: {waited!r}")
            verification = _wait_for_verification(
                connection,
                problem_id=problem_id,
                workspace_id=workspace_id,
                previous_id=previous_id,
            )
            if str(verification["id"]) != verification_id:
                raise RuntimeError(
                    "Agent verification ID does not match the persisted verification"
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
            if str(verification["sanity_status"] or "") != "passed":
                raise RuntimeError(
                    "Agent verification sanity checks did not pass"
                )
            _assert_tasks(
                connection,
                verification_id,
                special_verdicts=AGENT_SPECIAL_VERDICTS,
            )
            _assert_accepted_solution(connection, verification_id)
            _assert_artifact_refs(connection, verification_id)
            _assert_public_artifacts(client, verification_id)
        _assert_real_judgehost_executed(client)

        variant = os.environ["POLYGON_REPLICA_E2E_VARIANT"]
        if not product_tail:
            print(
                "e2e-real completed Judgehost compatibility verification "
                f"variant={variant} verification={verification_id}"
            )
            return

        _assert_agent_verification_detail(verification_id)
        committed = _agent_cli(
            "commit",
            "--problem",
            PROBLEM,
            "--message",
            COMMIT_MESSAGE,
        )
        head = str(committed.get("head") or "")
        if committed.get("status") != "ok" or not head:
            raise RuntimeError(f"Agent commit failed: {committed!r}")
        commit_status = _agent_cli(
            "commit-status",
            "--problem",
            PROBLEM,
            "--ref",
            head,
        )
        if commit_status != {
            "ref": head,
            "status": "published",
            "head": head,
            "remote_head": head,
        }:
            raise RuntimeError(f"Agent commit status is wrong: {commit_status!r}")
        refreshed = client.get(f"/problems/{PROBLEM}/workspace")
        if refreshed.status_code != 200:
            raise RuntimeError(
                f"workspace status refresh returned {refreshed.status_code}"
            )

        with _connect() as connection:
            persisted_head = _assert_commit(connection)
        if persisted_head != head:
            raise RuntimeError("Agent commit response differs from the persisted head")

        native_job_id, native_archive = _agent_export("native")
        icpc_job_id, icpc_archive = _agent_export("icpc")

        contest_job_id = start_contest_pdf(
            client,
            post_redirect=_post,
            problem=PROBLEM,
        )
        contest_job = wait_for_contest_job(client, contest_job_id)
        with _connect() as connection:
            artifact_id, materialization_verification_id = assert_contest_pdf(
                client,
                connection,
                problem=PROBLEM,
                job_id=contest_job_id,
                job=contest_job,
                expected_head=head,
            )
        collaboration_head = exercise_role_pages_and_collaboration(
            admin=client,
            client_factory=_client,
            register_user=_register_user,
            post_redirect=_post,
            problem=PROBLEM,
            contest=CONTEST,
            verification_id=verification_id,
            initial_head=head,
        )
    print(
        "e2e-real completed deployment, sample preview, verification, commit, "
        "Native/ICPC exports, and contest PDF export "
        f"variant={variant} "
        f"sample_verification={sample_verification_id} "
        f"verification={verification_id} head={head} "
        f"native_job={native_job_id} native_archive={native_archive} "
        f"icpc_job={icpc_job_id} icpc_archive={icpc_archive} "
        f"materialization_verification={materialization_verification_id} "
        f"contest_job={contest_job_id} artifact={artifact_id} "
        f"collaboration_head={collaboration_head}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
        return
    verify_deployment()


if __name__ == "__main__":
    main()
