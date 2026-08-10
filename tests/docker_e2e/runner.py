"""Black-box assertions for the isolated app + mock-Judgehost Compose stack."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx

from domjudge_contract import (
    BOOTSTRAP_FILENAME,
    MOCK_STATE_FILENAME,
    UPSTREAM_PEELED_COMMIT,
    require_approval,
    state_dir,
)


TERMINAL_VERIFICATION_STATUSES = frozenset({"ok", "failed"})
BLOB_REF = re.compile(r"^blob://sha256/([0-9a-f]{64})$")


def _load_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load E2E state object: {path}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"E2E state is not an object: {path}")
    return dict(raw)


def _connect() -> sqlite3.Connection:
    database = Path(os.environ["POLYGON_REPLICA_E2E_DB"]).resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _latest_verification(
    connection: sqlite3.Connection,
    *,
    problem_id: int,
    workspace_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id,status,fail_reason,error,created_at,finished_at
        FROM verifications
        WHERE problem_id=? AND workspace_id=?
        ORDER BY created_at DESC,id DESC
        LIMIT 1
        """,
        [problem_id, workspace_id],
    ).fetchone()


def _wait_for_verification(
    connection: sqlite3.Connection,
    *,
    problem_id: int,
    workspace_id: int,
    previous_id: str,
    timeout_sec: float = 150.0,
) -> sqlite3.Row:
    deadline = time.monotonic() + timeout_sec
    last: sqlite3.Row | None = None
    while time.monotonic() < deadline:
        last = _latest_verification(
            connection,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        if (
            last is not None
            and str(last["id"]) != previous_id
            and str(last["status"]) in TERMINAL_VERIFICATION_STATUSES
        ):
            return last
        time.sleep(0.1)
    detail = None if last is None else dict(last)
    raise RuntimeError(f"verification did not reach a terminal state: {detail!r}")


def _read_blob(ref: str) -> bytes:
    match = BLOB_REF.fullmatch(ref)
    if match is None:
        raise RuntimeError(f"invalid runtime blob ref: {ref!r}")
    identity = match.group(1)
    cache_root = Path(os.environ["POLYGON_REPLICA_E2E_CACHE_ROOT"]).resolve()
    path = cache_root / "runtime" / "blobs" / identity[:2] / identity
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"runtime blob is unavailable: {ref}")
    return path.read_bytes()


def _assert_tasks(connection: sqlite3.Connection, verification_id: str) -> None:
    rows = connection.execute(
        """
        SELECT task_kind,test_name,final_status,result_json
        FROM verification_tasks
        WHERE verification_id=?
        ORDER BY created_at,id
        """,
        [verification_id],
    ).fetchall()
    by_kind = {str(row["task_kind"]): row for row in rows}
    for task_kind in ("generate-input", "main-correct"):
        row = by_kind.get(task_kind)
        if row is None:
            raise RuntimeError(f"verification omitted task kind {task_kind}")
        if str(row["final_status"]) != "done":
            raise RuntimeError(f"{task_kind} did not finish successfully: {dict(row)!r}")
        try:
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{task_kind} persisted invalid result JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"{task_kind} result is not canonical")
        if set(result) != {"outcome", "compile", "passes", "warnings"}:
            raise RuntimeError(f"{task_kind} result has a non-canonical shape: {sorted(result)!r}")
        passes = result["passes"]
        if not isinstance(passes, list) or not passes:
            raise RuntimeError(f"{task_kind} lost its pass evidence")
        first_pass = passes[0]
        if not isinstance(first_pass, dict) or not isinstance(first_pass.get("artifacts"), dict):
            raise RuntimeError(f"{task_kind} lost its pass artifact evidence")
        artifacts = first_pass["artifacts"]
        if not str(artifacts.get("output_ref") or "").startswith("blob://sha256/"):
            raise RuntimeError(f"{task_kind} pass output ref is missing")


def _assert_artifact_refs(connection: sqlite3.Connection, verification_id: str) -> None:
    row = connection.execute(
        """
        SELECT input_ref,answer_ref
        FROM verification_artifact_refs
        WHERE verification_id=? AND test_name='001.in'
        """,
        [verification_id],
    ).fetchone()
    if row is None:
        raise RuntimeError("verification omitted generated-test artifact refs")
    input_ref = str(row["input_ref"])
    answer_ref = str(row["answer_ref"])
    if _read_blob(input_ref) != b"7\n":
        raise RuntimeError("generator input ref does not resolve to the mock output")
    if _read_blob(answer_ref) != b"49\n":
        raise RuntimeError("main-correct answer ref does not resolve to the mock output")


def _wait_for_mock_evidence(timeout_sec: float = 10.0) -> dict[str, object]:
    path = state_dir() / MOCK_STATE_FILENAME
    deadline = time.monotonic() + timeout_sec
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = _load_object(path)
        events = latest.get("events")
        completed = (
            [
                event
                for event in events
                if isinstance(event, dict) and event.get("kind") == "completed"
            ]
            if isinstance(events, list)
            else []
        )
        sources = {str(event.get("source") or "") for event in completed}
        if {
            "gen.py",
            "main.cpp",
            "sanity_empty_output.py",
            "sanity_unicode_output.py",
        }.issubset(sources):
            return latest
        time.sleep(0.05)
    raise RuntimeError(f"mock did not persist expected completion evidence: {latest!r}")


def _assert_mock_evidence(
    state: dict[str, object],
    *,
    approved_source_sha256: object,
) -> None:
    if state.get("domjudge_commit") != UPSTREAM_PEELED_COMMIT:
        raise RuntimeError("mock evidence is not tied to the pinned DOMjudge commit")
    if state.get("source_sha256") != approved_source_sha256:
        raise RuntimeError("mock evidence is not tied to the approved DOMjudge source blob")
    if state.get("error"):
        raise RuntimeError(f"mock Judgehost failed: {state['error']}")
    events = state.get("events")
    if not isinstance(events, list):
        raise RuntimeError("mock evidence has no event list")
    completed = [
        event
        for event in events
        if isinstance(event, dict) and event.get("kind") == "completed"
    ]
    if not completed:
        raise RuntimeError("mock completed no Judgehost cases")
    expected_results = {
        "gen.py": "correct",
        "main.cpp": "correct",
        "sanity_empty_output.py": "wrong-answer",
        "sanity_unicode_output.py": "wrong-answer",
    }
    observed_results = {
        str(event.get("source") or ""): str(event.get("runresult") or "")
        for event in completed
    }
    missing_results = {
        source: expected
        for source, expected in expected_results.items()
        if observed_results.get(source) != expected
    }
    if missing_results:
        raise RuntimeError(
            "mock did not exercise the expected verification and sanity cases: "
            f"expected={missing_results!r}, observed={observed_results!r}"
        )
    for event in completed:
        if type(event.get("ack")) is not int or event["ack"] != 1:
            raise RuntimeError(f"mock observed a non-canonical callback ACK: {event!r}")
        executable_files = event.get("executable_files")
        if not isinstance(executable_files, dict) or set(executable_files) != {
            "compile",
            "run",
            "compare",
        }:
            raise RuntimeError(f"mock skipped an official executable download: {event!r}")
        if set(event.get("testcase_files") or []) != {"input", "output"}:
            raise RuntimeError(f"mock skipped the official testcase files: {event!r}")


def main() -> None:
    approval = require_approval()
    bootstrap = _load_object(state_dir() / BOOTSTRAP_FILENAME)
    if bootstrap.get("domjudge_commit") != approval["commit"]:
        raise RuntimeError("bootstrap and mock contract approvals differ")

    problem_id = int(bootstrap["problem_id"])
    workspace_id = int(bootstrap["workspace_id"])
    with _connect() as connection:
        previous = _latest_verification(
            connection,
            problem_id=problem_id,
            workspace_id=workspace_id,
        )
        previous_id = "" if previous is None else str(previous["id"])

        origin = os.environ["POLYGON_REPLICA_E2E_APP_ORIGIN"].rstrip("/")
        problem = str(bootstrap["problem"])
        session_cookie = (
            f"{bootstrap['session_cookie_name']}={bootstrap['session_token']}"
        )
        response = httpx.post(
            f"{origin}/problems/{problem}/verification/start",
            data={"page": "tests"},
            headers={
                "Origin": origin,
                "Cookie": session_cookie,
            },
            follow_redirects=False,
            timeout=15.0,
        )
        if response.status_code != 303:
            raise RuntimeError(
                f"verification start returned {response.status_code}: {response.text[:500]}"
            )

        verification = _wait_for_verification(
            connection,
            problem_id=problem_id,
            workspace_id=workspace_id,
            previous_id=previous_id,
        )
        if str(verification["status"]) != "ok":
            raise RuntimeError(f"verification failed: {dict(verification)!r}")
        if str(verification["fail_reason"] or "") or str(
            verification["error"] or ""
        ):
            raise RuntimeError(f"successful verification retained a failure: {dict(verification)!r}")
        verification_id = str(verification["id"])
        _assert_tasks(connection, verification_id)
        _assert_artifact_refs(connection, verification_id)

    mock_state = _wait_for_mock_evidence()
    _assert_mock_evidence(
        mock_state,
        approved_source_sha256=approval["source_sha256"],
    )
    print(
        "Docker E2E passed with official-source-approved mock Judgehost "
        f"commit={UPSTREAM_PEELED_COMMIT} verification={verification_id}"
    )


if __name__ == "__main__":
    main()
