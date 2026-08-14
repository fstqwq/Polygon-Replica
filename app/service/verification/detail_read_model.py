"""Canonical verification-detail facts assembled from one SQLite snapshot."""

import json
import re
from pathlib import Path
from typing import TypedDict, cast

from app.service.judgehost.callback.case_result import decode_case_test_row
from app.service.platform.error_text import bounded_display_text
from app.service.verification.lifecycle import (
    VerificationSnapshot,
    VerificationSnapshotRecord,
)
import app.service.verification.read_model
from app.service.verification.task_store import (
    VerificationTaskReadRow,
    VerificationTaskRow,
)
from app.service.verification.types import VerificationTaskStatus


_SOLUTION_TASK_KINDS = frozenset(("solution-run", "main-correct"))
_TEST_NAME_RE = re.compile(r"^(\d+)\.in$")


class VerificationDetailReadModel(TypedDict):
    record: VerificationSnapshotRecord
    details: dict[str, object]
    tasks: list[VerificationTaskRow]
    has_task_graph: bool
    mode: str
    program_ids: list[str]
    program_rows: dict[str, dict[str, object]]
    task_status_by_program_and_test: dict[tuple[str, str], str]
    task_counts: dict[str, object]
    running_tasks: list[dict[str, str]]


def _test_order(test_name: str) -> tuple[int, str]:
    token = Path(test_name).name
    match = _TEST_NAME_RE.fullmatch(token)
    return (int(match.group(1)), token) if match is not None else (10**9, token)


def _late_diagnostic_text(row: VerificationTaskRow, limit: int) -> str:
    rendered = str(row.get("late_diagnostic_text") or "")
    if rendered:
        return bounded_display_text(rendered, limit_bytes=limit)
    messages: list[str] = []
    raw_items = row.get("late_diagnostics")
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            text = str(raw_item.get("text") or "")
            if not text:
                continue
            label = (
                f'late {raw_item.get("kind") or "diagnostic"} from '
                f'{raw_item.get("hostname") or "unknown host"}'
            )
            received_at = str(raw_item.get("received_at") or "")
            if received_at:
                label += f" at {received_at}"
            messages.append(f"[{label}]\n{text}")
    return bounded_display_text("\n\n".join(messages), limit_bytes=limit)


def _program_status(rows: list[VerificationTaskRow]) -> str:
    statuses = [row["status"] for row in rows]
    for value, display in (
        (VerificationTaskStatus.LEASED, "running"),
        (VerificationTaskStatus.QUEUED, "queued"),
        (VerificationTaskStatus.PENDING, "pending"),
        (VerificationTaskStatus.FAILED, "failed"),
        (VerificationTaskStatus.CANCELLED, "cancelled"),
    ):
        if value in statuses:
            return display
    if rows and all(row["status"] == VerificationTaskStatus.DONE for row in rows):
        return "ok"
    return "pending"


def _program_rows(
    rows: list[VerificationTaskRow],
    *,
    record: VerificationSnapshotRecord,
    details: dict[str, object],
    mode: str,
    pass_limit: int,
    display_limit: int,
) -> dict[str, dict[str, object]]:
    try:
        run_config = json.loads(str(details.get("run_config_json") or ""))
    except (TypeError, ValueError):
        run_config = {}
    if not isinstance(run_config, dict):
        run_config = {}
    if mode in {"pass-fail", "interactive"}:
        run_config = {**run_config, "mode": mode, "pass_limit": pass_limit}
    else:
        run_config = {}
    grouped: dict[str, list[VerificationTaskRow]] = {}
    for row in rows:
        if row["task_kind"] not in _SOLUTION_TASK_KINDS:
            continue
        program_id = row["program_id"]
        if program_id and row["source_path"]:
            grouped.setdefault(program_id, []).append(row)

    values: dict[str, dict[str, object]] = {}
    verification_error = str(details.get("error") or record["fail_reason"])
    for program_id, unsorted_rows in grouped.items():
        program_tasks = sorted(
            unsorted_rows,
            key=lambda row: (_test_order(row["test_name"]), row["id"]),
        )
        tests: list[dict[str, object]] = []
        compile_log = ""
        compile_diagnostics: list[dict[str, object]] = []
        error_text = ""
        late_messages: list[str] = []
        max_time_ms = 0
        max_memory_kb = 0
        for row in program_tasks:
            if (
                row["status"]
                in {
                    VerificationTaskStatus.DONE,
                    VerificationTaskStatus.FAILED,
                }
                and row["verdict"].upper() != "SK"
            ):
                test_row = decode_case_test_row(
                    row["result"],
                    test_name=row["test_name"],
                )
                late = _late_diagnostic_text(row, display_limit)
                if late:
                    test_row["message"] = bounded_display_text(
                        "\n\n".join(
                            item for item in (str(test_row.get("message") or ""), late) if item
                        ),
                        limit_bytes=display_limit,
                    )
                test_row["late_diagnostics"] = list(
                    cast(list[object], row.get("late_diagnostics") or [])
                )
                test_row["late_diagnostic_text"] = late
                tests.append(test_row)
                runtime_ms = int(test_row.get("time_ms") or 0)
                max_time_ms = max(
                    max_time_ms,
                    int(test_row.get("time_user_ms") or runtime_ms),
                )
                max_memory_kb = max(
                    max_memory_kb,
                    int(test_row.get("memory_kb") or 0),
                )
            if not compile_log and row["compile_log"]:
                compile_log = row["compile_log"]
            try:
                diagnostics = json.loads(row["diagnostics_json"])
            except (TypeError, ValueError):
                diagnostics = []
            if isinstance(diagnostics, list):
                compile_diagnostics.extend(cast(list[dict[str, object]], diagnostics))
            if not error_text and row["error_text"]:
                error_text = row["error_text"]
            late = _late_diagnostic_text(row, display_limit)
            if late and late not in late_messages:
                late_messages.append(late)
        if late_messages:
            error_text = bounded_display_text(
                "\n\n".join(item for item in (error_text, *late_messages) if item),
                limit_bytes=display_limit,
            )
        status = _program_status(program_tasks)
        first = program_tasks[0]
        summary: dict[str, object] = {
            "mode": mode,
            "source": first["source_path"],
            "task_kind": first["task_kind"],
            "expected_behavior": first["expected_behavior"],
            "tests_total": len(program_tasks),
            "tests": tests,
            "compile_log": compile_log,
            "compile_diagnostics": compile_diagnostics,
            "error": error_text,
            "usage": {
                "tests": len(tests),
                "time_ms_total": max_time_ms,
                "time_user_ms_total": max_time_ms,
                "time_wall_ms_total": max_time_ms,
                "memory_kb_peak": max_memory_kb,
            },
        }
        if run_config:
            summary["run_config"] = run_config
        if any(row["status"] == VerificationTaskStatus.CANCELLED for row in program_tasks):
            summary["cancelled"] = True
            if record["status"] in {"failed", "cancelled"} and verification_error:
                summary["error"] = summary["error"] or verification_error
        values[program_id] = {
            "id": program_id,
            "artifact_verification_id": record["id"],
            "mode": mode,
            "status": status,
            "source_label": first["source_path"],
            "summary": summary,
            "created_at": record["created_at"],
            "finished_at": max(
                (str(row["finished_at"] or "") for row in program_tasks),
                default=record["finished_at"],
            )
            or record["finished_at"],
        }
    return values


def build_verification_detail_read_model(
    snapshot: VerificationSnapshot,
    *,
    display_limit: int,
) -> VerificationDetailReadModel:
    record = snapshot["record"]
    tasks = cast(list[VerificationTaskRow], snapshot["tasks"])
    read_rows = cast(list[VerificationTaskReadRow], tasks)
    runtime_counts = app.service.verification.read_model.task_counts(read_rows)
    details = {
        **snapshot["detail"],
        "verification_id": record["id"],
        "artifact_verification_id": record["id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "finished_at": record["finished_at"],
        "task_graph": bool(tasks),
        "task_counts": runtime_counts,
        "running_tasks": app.service.verification.read_model.running_tasks(read_rows),
        "source_paths": app.service.verification.read_model.solution_source_paths(read_rows),
        "program_ids": app.service.verification.read_model.program_ids(read_rows),
        "has_running": bool(
            int(runtime_counts["pending"])
            or int(runtime_counts["queued"])
            or int(runtime_counts["running"])
        ),
        "test_names": list(dict.fromkeys(row["test_name"] for row in tasks if row["test_name"])),
    }
    mode = str(details.get("mode") or "")
    if mode not in {"pass-fail", "interactive"}:
        mode = "malformed"
    program_ids: list[str] = []
    status_by_case: dict[tuple[str, str], str] = {}
    for row in tasks:
        if row["task_kind"] not in _SOLUTION_TASK_KINDS:
            continue
        program_id = row["program_id"]
        if program_id and program_id not in program_ids:
            program_ids.append(program_id)
        if program_id and row["test_name"]:
            status_by_case[(program_id, row["test_name"])] = row["status"]
    return {
        "record": record,
        "details": details,
        "tasks": tasks,
        "has_task_graph": bool(tasks),
        "mode": mode,
        "program_ids": program_ids,
        "program_rows": _program_rows(
            tasks,
            record=record,
            details=details,
            mode=mode,
            pass_limit=int(details.get("pass_limit") or 1),
            display_limit=display_limit,
        ),
        "task_status_by_program_and_test": status_by_case,
        "task_counts": runtime_counts,
        "running_tasks": cast(
            list[dict[str, str]],
            details["running_tasks"],
        ),
    }
