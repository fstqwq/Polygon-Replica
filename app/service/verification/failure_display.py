from __future__ import annotations

from pathlib import Path

from app.service.platform.error_text import bounded_display_text
from app.service.verification.result_match import verification_solution_match
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore


def verification_solution_failure_hint(
    source_path: str,
    reason: str,
    error_text: str = "",
) -> str:
    source_label = Path(source_path).name if source_path else ""
    if not source_label:
        source_label = "solution"
    rich_error = bounded_display_text(error_text)
    if reason and rich_error:
        detail = f"{reason}: {rich_error}"
    elif reason:
        detail = reason
    elif rich_error:
        detail = rich_error
    else:
        detail = "verification mismatch"
    return bounded_display_text(f"{source_label}: {detail}")


def _task_status(rows: list[VerificationTaskRow]) -> str:
    statuses = {row["status"] for row in rows}
    if statuses & {
        VerificationTaskStore.TASK_PENDING,
        VerificationTaskStore.TASK_QUEUED,
        VerificationTaskStore.TASK_LEASED,
    }:
        return "running"
    if statuses & {
        VerificationTaskStore.TASK_FAILED,
        VerificationTaskStore.TASK_CANCELLED,
    }:
        return "failed"
    return "ok"


def verification_task_failure_hint(
    task_store: VerificationTaskStore,
    verification_id: str,
) -> str:
    grouped: dict[str, list[VerificationTaskRow]] = {}
    order: list[str] = []
    for row in task_store.list_rows(verification_id):
        if row["task_kind"] != "solution-run":
            continue
        logical_run_id = row["logical_run_id"]
        if logical_run_id not in grouped:
            grouped[logical_run_id] = []
            order.append(logical_run_id)
        grouped[logical_run_id].append(row)
    for logical_run_id in order:
        rows = grouped[logical_run_id]
        first = rows[0]
        summary: dict[str, object] = {
            "tests": [
                {"test": row["test_name"], "verdict": row["verdict"]}
                for row in rows
                if row["verdict"] and row["verdict"].upper() != "SK"
            ],
            "error": next((row["error_text"] for row in rows if row["error_text"]), ""),
        }
        _matched, completed, _passed, reason = verification_solution_match(
            first["expected_behavior"],
            _task_status(rows),
            summary,
        )
        error_text = str(summary["error"])
        if completed and reason:
            return verification_solution_failure_hint(first["source_path"], reason)
        if completed and error_text:
            return verification_solution_failure_hint(
                first["source_path"],
                "",
                error_text,
            )
    return ""
