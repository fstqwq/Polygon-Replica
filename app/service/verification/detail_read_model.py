"""Canonical verification-detail facts assembled from one SQLite snapshot."""

import json
import re
from pathlib import Path
from typing import TypedDict

from app.service.execution.codec import compile_diagnostics_payload
from app.service.execution.model import JsonValue
from app.service.judgehost.domjudge.case_result import decode_case_test_row
from app.service.platform.error_text import bounded_display_text
from app.service.verification.lifecycle import (
    VerificationSnapshot,
    VerificationSnapshotRecord,
)
import app.service.verification.read_model
from app.service.verification.read_model import TaskCounts
from app.service.verification.types import VerificationCaseTestRow, VerificationProgramDetailSummary, VerificationTaskRow
from app.service.verification.types import VerificationDetail, VerificationStatus, VerificationTaskStatus

_SOLUTION_TASK_KINDS = frozenset(("solution-run", "main-correct"))
_TEST_NAME_RE = re.compile(r"^(\d+)\.in$")


class VerificationProgramDetailRow(TypedDict):
    id: str
    artifact_verification_id: str
    mode: str
    status: str
    source_label: str
    summary: VerificationProgramDetailSummary
    created_at: str
    finished_at: str


class VerificationPageDetail(VerificationDetail):
    verification_id: str
    artifact_verification_id: str
    status: VerificationStatus
    created_at: str
    finished_at: str
    task_graph: bool
    task_counts: TaskCounts
    running_tasks: list[dict[str, str]]
    program_ids: list[str]
    has_running: bool
    test_names: list[str]


class VerificationDetailReadModel(TypedDict):
    record: VerificationSnapshotRecord
    details: VerificationPageDetail
    tasks: list[VerificationTaskRow]
    has_task_graph: bool
    mode: str
    program_ids: list[str]
    program_rows: dict[str, VerificationProgramDetailRow]
    task_status_by_program_and_test: dict[tuple[str, str], str]
    task_counts: TaskCounts
    running_tasks: list[dict[str, str]]


class VerificationTestDetailReadModel(TypedDict):
    record: VerificationSnapshotRecord
    details: VerificationDetail
    test_name: str
    mode: str
    tasks: list[VerificationTaskRow]
    cases: list[VerificationTaskRow]


def build_verification_test_detail_read_model(
    snapshot: VerificationSnapshot, *, test_name: str, program_id: str | None,
) -> VerificationTestDetailReadModel:
    tasks = snapshot["tasks"]
    details = snapshot["detail"]
    mode = str(details.get("mode") or "")
    return {
        "record": snapshot["record"],
        "details": details,
        "test_name": test_name,
        "mode": mode if mode in {"pass-fail", "interactive"} else "malformed",
        "tasks": tasks,
        "cases": [
            row for row in tasks
            if row["test_name"] == test_name
            and row["task_kind"] in _SOLUTION_TASK_KINDS
            and (not program_id or row["program_id"] == program_id)
        ],
    }


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


def verification_case_test_row(
    row: VerificationTaskRow, *, display_limit: int, include_pass_details: bool = True,
) -> VerificationCaseTestRow:
    test_row = decode_case_test_row(
        row["result"], test_name=row["test_name"], include_passes=include_pass_details,
    )
    late = _late_diagnostic_text(row, display_limit)
    if late:
        test_row["message"] = bounded_display_text(
            "\n\n".join(value for value in (str(test_row.get("message") or ""), late) if value),
            limit_bytes=display_limit,
        )
    return {
        **test_row,
        "late_diagnostics": list(row.get("late_diagnostics") or []),
        "late_diagnostic_text": late,
    }


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
    details: VerificationDetail,
    mode: str,
    pass_limit: int,
    display_limit: int,
    include_pass_details: bool,
) -> dict[str, VerificationProgramDetailRow]:
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

    values: dict[str, VerificationProgramDetailRow] = {}
    verification_error = str(details.get("error") or record["fail_reason"])
    for program_id, unsorted_rows in grouped.items():
        program_tasks = sorted(
            unsorted_rows,
            key=lambda row: (_test_order(row["test_name"]), row["id"]),
        )
        tests: list[VerificationCaseTestRow] = []
        compile_log = ""
        compile_diagnostics: list[dict[str, JsonValue]] = []
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
                test_row = verification_case_test_row(
                    row, display_limit=display_limit,
                    include_pass_details=include_pass_details,
                )
                tests.append(test_row)
                max_time_ms = max(max_time_ms, test_row["time_user_ms"])
                max_memory_kb = max(max_memory_kb, test_row["memory_kb"])
            if not compile_log and row["compile_log"]:
                compile_log = row["compile_log"]
            compile_diagnostics.extend(
                compile_diagnostics_payload(row["result"].compile.diagnostics)
            )
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
        tests_skipped = sum(
            1 for row in program_tasks if row["verdict"].upper() == "SK"
        )
        first = program_tasks[0]
        summary: VerificationProgramDetailSummary = {
            "mode": mode,
            "source": first["source_path"],
            "task_kind": first["task_kind"],
            "expected_behavior": first["expected_behavior"],
            "tests_total": len(program_tasks),
            "tests_skipped": tests_skipped,
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
        if any(
            row["status"] == VerificationTaskStatus.CANCELLED for row in program_tasks
        ):
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
    include_pass_details: bool = True,
) -> VerificationDetailReadModel:
    record = snapshot["record"]
    tasks = snapshot["tasks"]
    runtime_counts = app.service.verification.read_model.task_counts(tasks)
    active_tasks = app.service.verification.read_model.running_tasks(tasks)
    details: VerificationPageDetail = {
        **snapshot["detail"],
        "verification_id": record["id"],
        "artifact_verification_id": record["id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "finished_at": record["finished_at"],
        "task_graph": bool(tasks),
        "task_counts": runtime_counts,
        "running_tasks": active_tasks,
        "source_paths": app.service.verification.read_model.solution_source_paths(
            tasks
        ),
        "program_ids": app.service.verification.read_model.program_ids(tasks),
        "has_running": bool(
            runtime_counts["pending"]
            or runtime_counts["queued"]
            or runtime_counts["running"]
        ),
        "test_names": list(
            dict.fromkeys(row["test_name"] for row in tasks if row["test_name"])
        ),
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
            pass_limit=details.get("pass_limit", 1),
            display_limit=display_limit,
            include_pass_details=include_pass_details,
        ),
        "task_status_by_program_and_test": status_by_case,
        "task_counts": runtime_counts,
        "running_tasks": active_tasks,
    }
