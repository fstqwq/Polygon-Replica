"""Black-box assertions for the isolated app + mock-Judgehost Compose stack."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx

from judgehost_protocol import (
    BOOTSTRAP_FILENAME,
    MOCK_STATE_FILENAME,
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
        SELECT id,kind,status,fail_reason,error,sanity_status,created_at,finished_at
        FROM verifications
        WHERE problem_id=? AND workspace_id=?
        ORDER BY created_at DESC,id DESC
        LIMIT 1
        """,
        [problem_id, workspace_id],
    ).fetchone()


def _latest_preview(
    connection: sqlite3.Connection,
    *,
    problem_id: int,
    workspace_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id,status,verification_id,summary_json,created_at
        FROM previews
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
    timeout_sec: float = 300.0,
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

    special_rows = connection.execute(
        """
        SELECT source_path,final_status,result_json
        FROM verification_tasks
        WHERE verification_id=? AND source_path IN ('solutions/re.py','solutions/ce.cpp')
        """,
        [verification_id],
    ).fetchall()
    special_by_source = {str(row["source_path"]): row for row in special_rows}
    for source, expected_verdict in {
        "solutions/re.py": "RE",
        "solutions/ce.cpp": "CE",
    }.items():
        row = special_by_source.get(source)
        if row is None or str(row["final_status"]) != "done":
            raise RuntimeError(f"expected {source} task did not complete: {row!r}")
        result = json.loads(str(row["result_json"]))
        outcome = result.get("outcome") if isinstance(result, dict) else None
        verdict = outcome.get("verdict") if isinstance(outcome, dict) else None
        if verdict != expected_verdict:
            raise RuntimeError(
                f"expected {source} verdict {expected_verdict}, got {verdict!r}"
            )


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


def _assert_preview_sample_materialization(
    connection: sqlite3.Connection,
    *,
    problem_id: int,
    workspace_id: int,
    verification_id: str,
) -> None:
    rows = connection.execute(
        """
        SELECT task_kind,final_status
        FROM verification_tasks
        WHERE verification_id=? AND task_kind IN ('generate-input','main-correct')
        ORDER BY task_kind
        """,
        [verification_id],
    ).fetchall()
    statuses = {
        str(row["task_kind"]): str(row["final_status"])
        for row in rows
    }
    if statuses != {"generate-input": "done", "main-correct": "done"}:
        raise RuntimeError(
            f"preview sample verification did not materialize its core tasks: {statuses!r}"
        )
    _assert_artifact_refs(connection, verification_id)

    preview = _latest_preview(
        connection,
        problem_id=problem_id,
        workspace_id=workspace_id,
    )
    if preview is None or str(preview["status"]) != "ok":
        raise RuntimeError(f"preview did not finish successfully: {preview!r}")
    if str(preview["verification_id"] or "") != verification_id:
        raise RuntimeError(
            "preview did not retain the sample verification identity: "
            f"{dict(preview)!r}"
        )
    preview_root = (
        Path(os.environ["POLYGON_REPLICA_E2E_CACHE_ROOT"]).resolve()
        / "artifacts"
        / "previews"
        / str(preview["id"])
    )
    preview_pdf = preview_root / "statement_preview" / "statement.pdf"
    if not preview_pdf.is_file() or not preview_pdf.read_bytes().startswith(b"%PDF-"):
        raise RuntimeError(f"preview PDF is unavailable or invalid: {preview_pdf}")
    latex_log = preview_root / "logs" / "latex.log"
    log_text = latex_log.read_text(encoding="utf-8", errors="replace")
    missing_samples = {
        filename
        for filename in ("sample.001.in", "sample.001.ans")
        if filename not in log_text
    }
    if missing_samples:
        raise RuntimeError(
            "preview TeX compile did not read the materialized sample files: "
            f"{sorted(missing_samples)!r}"
        )
    try:
        summary = json.loads(str(preview["summary_json"]))
    except json.JSONDecodeError as exc:
        raise RuntimeError("preview persisted invalid summary JSON") from exc
    sample_sync = summary.get("sample_sync") if isinstance(summary, dict) else None
    if (
        not isinstance(sample_sync, dict)
        or sample_sync.get("verification_id") != verification_id
        or int(sample_sync.get("copied") or 0) != 1
    ):
        raise RuntimeError(
            f"preview did not persist sample materialization evidence: {sample_sync!r}"
        )


def _assert_late_diagnostics(
    connection: sqlite3.Connection,
    verification_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT t.final_status,t.result_json,d.snapshot_json
        FROM verification_tasks t
        JOIN verification_task_diagnostics d ON d.task_id=t.id
        WHERE t.verification_id=? AND t.source_path='solutions/re.py'
        """,
        [verification_id],
    ).fetchone()
    if row is None or str(row["final_status"]) != "done":
        raise RuntimeError("late diagnostics are not attached to the terminal RE task")
    result = json.loads(str(row["result_json"]))
    snapshot = json.loads(str(row["snapshot_json"]))
    items = snapshot.get("items") if isinstance(snapshot, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("late diagnostic snapshot has an invalid shape")
    kinds = [
        str(item.get("kind") or "")
        for item in items
        if isinstance(item, dict)
    ]
    if kinds != ["debug-info", "internal-error"]:
        raise RuntimeError(f"late diagnostic ordering or kinds are wrong: {items!r}")
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("hostname") != "mock-domjudge-9-0-1"
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("digest") or ""))
            or not str(item.get("received_at") or "")
        ):
            raise RuntimeError(f"late diagnostic item is incomplete: {item!r}")
    text_by_kind = {
        str(item["kind"]): str(item["text"])
        for item in items
        if isinstance(item, dict)
    }
    if "late debug-info from mock" not in text_by_kind.get("debug-info", ""):
        raise RuntimeError("late debug-info text was not preserved")
    if "late internal-error from mock" not in text_by_kind.get("internal-error", ""):
        raise RuntimeError("late internal-error text was not preserved")
    result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if "late debug-info from mock" in result_text or "late internal-error from mock" in result_text:
        raise RuntimeError("late diagnostics amended the canonical execution result")


def _assert_active_internal_error_sanity(
    connection: sqlite3.Connection,
    verification_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT status,checked_count
        FROM verification_sanity_checks
        WHERE verification_id=? AND check_name='unicode_output_stability'
        """,
        [verification_id],
    ).fetchone()
    if row is None or str(row["status"]) != "failed":
        raise RuntimeError(
            f"active internal-error did not fail the selected sanity check: {row!r}"
        )
    message = connection.execute(
        """
        SELECT message
        FROM verification_sanity_check_messages
        WHERE verification_id=? AND check_name='unicode_output_stability'
        ORDER BY ordinal
        LIMIT 1
        """,
        [verification_id],
    ).fetchone()
    if message is None or "active internal-error from mock" not in str(
        message["message"]
    ):
        raise RuntimeError(
            f"active internal-error detail was not retained by sanity: {message!r}"
        )


def _wait_for_mock_evidence(
    timeout_sec: float = 10.0,
    *,
    minimum_event_count: int = 0,
) -> dict[str, object]:
    path = state_dir() / MOCK_STATE_FILENAME
    deadline = time.monotonic() + timeout_sec
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = _load_object(path)
        events = latest.get("events")
        new_events = (
            events[minimum_event_count:]
            if isinstance(events, list)
            else []
        )
        relevant = [
            event
            for event in new_events
            if isinstance(event, dict)
            and event.get("kind") in {
                "completed",
                "compile-error",
                "internal-error",
            }
        ]
        sources = {str(event.get("source") or "") for event in relevant}
        if isinstance(events, list) and len(events) > minimum_event_count and {
            "gen.py",
            "main.cpp",
            "re.py",
            "ce.cpp",
            "sanity_empty_output.py",
            "sanity_unicode_output.py",
        }.issubset(sources):
            return latest
        time.sleep(0.05)
    raise RuntimeError(f"mock did not persist expected completion evidence: {latest!r}")


def _assert_mock_evidence(state: dict[str, object]) -> None:
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
        "re.py": "run-error",
        "sanity_empty_output.py": "wrong-answer",
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
    unexpected_final_sources = {"ce.cpp", "sanity_unicode_output.py"}.intersection(
        observed_results
    )
    if unexpected_final_sources:
        raise RuntimeError(
            "CE or active internal-error incorrectly sent a final run report: "
            f"{sorted(unexpected_final_sources)!r}"
        )
    for event in completed:
        if type(event.get("ack")) is not int or event["ack"] != 1:
            raise RuntimeError(f"mock observed a non-canonical callback ACK: {event!r}")
        if type(event.get("duplicate_ack")) is not int or event["duplicate_ack"] != 1:
            raise RuntimeError(f"mock observed a non-idempotent duplicate ACK: {event!r}")
        executable_files = event.get("executable_files")
        if not isinstance(executable_files, dict) or set(executable_files) != {
            "compile",
            "run",
            "compare",
        }:
            raise RuntimeError(f"mock skipped a declared executable download: {event!r}")
        if set(event.get("testcase_files") or []) != {"input", "output"}:
            raise RuntimeError(f"mock skipped the declared testcase files: {event!r}")

    re_event = next(
        (event for event in completed if event.get("source") == "re.py"),
        None,
    )
    if (
        re_event is None
        or re_event.get("late_debug") is not True
        or type(re_event.get("late_internal_error_ack")) is not int
        or type(re_event.get("duplicate_late_internal_error_ack")) is not int
    ):
        raise RuntimeError(f"mock did not exercise late diagnostics: {re_event!r}")

    compile_events = [
        event
        for event in events
        if isinstance(event, dict) and event.get("kind") == "compile-error"
    ]
    if {str(event.get("source") or "") for event in compile_events} != {"ce.cpp"}:
        raise RuntimeError(f"mock did not exercise the CE update path: {compile_events!r}")
    if any(
        set(event.get("executable_files") or {}) != {"compile"}
        for event in compile_events
    ):
        raise RuntimeError(f"CE path continued after compile failure: {compile_events!r}")

    internal_events = [
        event
        for event in events
        if isinstance(event, dict) and event.get("kind") == "internal-error"
    ]
    if {str(event.get("source") or "") for event in internal_events} != {
        "sanity_unicode_output.py"
    }:
        raise RuntimeError(
            f"mock did not exercise the active internal-error path: {internal_events!r}"
        )
    if any(type(event.get("internal_error_ack")) is not int for event in internal_events):
        raise RuntimeError(
            f"active internal-error did not receive an integer response: {internal_events!r}"
        )


def main() -> None:
    bootstrap = _load_object(state_dir() / BOOTSTRAP_FILENAME)

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

        preview_response = httpx.post(
            f"{origin}/problems/{problem}/preview/run",
            data={"page": "statement", "language": "english"},
            headers={
                "Origin": origin,
                "Cookie": session_cookie,
            },
            follow_redirects=False,
            timeout=300.0,
        )
        if preview_response.status_code != 303:
            raise RuntimeError(
                "preview run returned "
                f"{preview_response.status_code}: {preview_response.text[:500]}"
            )
        sample_verification = _wait_for_verification(
            connection,
            problem_id=problem_id,
            workspace_id=workspace_id,
            previous_id=previous_id,
        )
        if (
            str(sample_verification["kind"]) != "sample"
            or str(sample_verification["status"]) != "ok"
        ):
            raise RuntimeError(
                "preview sample verification failed: "
                f"{dict(sample_verification)!r}"
            )
        sample_verification_id = str(sample_verification["id"])
        _assert_preview_sample_materialization(
            connection,
            problem_id=problem_id,
            workspace_id=workspace_id,
            verification_id=sample_verification_id,
        )
        previous_id = sample_verification_id

        response = httpx.post(
            f"{origin}/problems/{problem}/verification/start",
            data={"page": "tests"},
            headers={
                "Origin": origin,
                "Cookie": session_cookie,
            },
            follow_redirects=False,
            timeout=30.0,
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
        if str(verification["fail_reason"] or ""):
            raise RuntimeError(f"successful verification retained a failure: {dict(verification)!r}")
        if str(verification["sanity_status"] or "") != "failed":
            raise RuntimeError(
                f"active internal-error was not retained as sanity attention: {dict(verification)!r}"
            )
        verification_id = str(verification["id"])
        _assert_tasks(connection, verification_id)
        _assert_artifact_refs(connection, verification_id)
        _assert_active_internal_error_sanity(connection, verification_id)

    mock_state = _wait_for_mock_evidence()
    _assert_mock_evidence(mock_state)
    with _connect() as connection:
        _assert_late_diagnostics(connection, verification_id)
    print(
        "Docker E2E passed with mock Judgehost "
        f"verification={verification_id}"
    )


if __name__ == "__main__":
    main()
