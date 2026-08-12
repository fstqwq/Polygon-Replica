from pathlib import Path

from app.service.platform.error_text import bounded_display_text
from app.service.verification.result_match import verification_solution_match
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import VerificationTaskStatus


def verification_solution_failure_hint(
    source_path: str,
    reason: str,
    error_text: str = "",
    *,
    limit_bytes: int,
) -> str:
    source_label = Path(source_path).name if source_path else ""
    if not source_label:
        source_label = "solution"
    rich_error = bounded_display_text(error_text, limit_bytes=limit_bytes)
    if reason and rich_error:
        detail = f"{reason}: {rich_error}"
    elif reason:
        detail = reason
    elif rich_error:
        detail = rich_error
    else:
        detail = "verification mismatch"
    return bounded_display_text(
        f"{source_label}: {detail}",
        limit_bytes=limit_bytes,
    )


def _task_status(rows: list[VerificationTaskRow]) -> str:
    statuses = {row["status"] for row in rows}
    if statuses & {
        VerificationTaskStatus.PENDING,
        VerificationTaskStatus.QUEUED,
        VerificationTaskStatus.LEASED,
    }:
        return "running"
    if statuses & {
        VerificationTaskStatus.FAILED,
        VerificationTaskStatus.CANCELLED,
    }:
        return "failed"
    return "ok"


def verification_task_failure_hint(
    task_store: VerificationTaskStore,
    verification_id: str,
    *,
    limit_bytes: int,
) -> str:
    grouped: dict[str, list[VerificationTaskRow]] = {}
    order: list[str] = []
    for row in task_store.list_rows(verification_id):
        if row["task_kind"] != "solution-run":
            continue
        program_id = row["program_id"]
        if program_id not in grouped:
            grouped[program_id] = []
            order.append(program_id)
        grouped[program_id].append(row)
    for program_id in order:
        rows = grouped[program_id]
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
            return verification_solution_failure_hint(
                first["source_path"],
                reason,
                limit_bytes=limit_bytes,
            )
        if completed and error_text:
            return verification_solution_failure_hint(
                first["source_path"],
                "",
                error_text,
                limit_bytes=limit_bytes,
            )
    return ""
