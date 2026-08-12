from pathlib import Path
from typing import TypedDict

from app.impl.workspace.run_display import generation_status_text
from app.service.platform.error_text import bounded_display_text
from app.service.verification.task_store import VerificationTaskRow
from app.service.verification.types import VerificationTaskStatus


class TestGenerationView(TypedDict):
    source_kind: str
    command: str
    source_path: str
    display_source: str
    status: str
    verdict: str
    status_text: str
    feedback_display: str
    error_text: str
    terminal: bool
    tone: str
    status_label: str
    table_text: str
    detail: str
    alert_severity: str
    alert_message: str
    duplicate_of: str
    skipped: bool


def _owner_order(row: VerificationTaskRow) -> tuple[str, str]:
    return (str(row["finished_at"] or ""), str(row["id"] or ""))


def _duplicate_owner_by_task_id(
    generate_rows: list[VerificationTaskRow],
) -> dict[str, str]:
    rows_by_id = {str(row["id"]): row for row in generate_rows}
    owner_by_output_ref: dict[str, VerificationTaskRow] = {}
    for row in sorted(generate_rows, key=_owner_order):
        if row["status"] != VerificationTaskStatus.DONE:
            continue
        if str(row["verdict"]).upper() == "SK":
            continue
        output_ref = str(row["output_ref"])
        if output_ref and output_ref not in owner_by_output_ref:
            owner_by_output_ref[output_ref] = row

    owner_test_by_task_id: dict[str, str] = {}
    for row in generate_rows:
        if row["status"] != VerificationTaskStatus.DONE:
            continue
        if str(row["verdict"]).upper() != "SK":
            continue
        owner: VerificationTaskRow | None = None
        predecessor_task_id = str(row["predecessor_task_id"])
        if predecessor_task_id:
            candidate = rows_by_id.get(predecessor_task_id)
            if (
                candidate is not None
                and candidate["status"] == VerificationTaskStatus.DONE
                and str(candidate["verdict"]).upper() != "SK"
            ):
                owner = candidate
        if owner is None:
            output_ref = str(row["output_ref"])
            if output_ref:
                owner = owner_by_output_ref.get(output_ref)
        if owner is not None and str(owner["test_name"]):
            owner_test_by_task_id[str(row["id"])] = str(owner["test_name"])
    return owner_test_by_task_id


def _source_kind_and_command(
    row: VerificationTaskRow | None,
    tests_meta: dict[str, object],
) -> tuple[str, str, str]:
    source_path = "" if row is None else str(row["source_path"])
    if not source_path:
        source_path = str(tests_meta.get("source") or "")
    kind = str(tests_meta.get("kind") or "")
    if kind == "manual" or Path(source_path).name == "manual_validate.cpp":
        return ("manual", "", source_path)
    if kind in {"gen", "generated"} or row is not None:
        return ("generated", str(tests_meta.get("command") or "gen"), source_path)
    return ("", "", source_path)


def _status_presentation(status: str, verdict: str) -> tuple[str, str, str]:
    if status == VerificationTaskStatus.LEASED:
        return ("running", "running", ".. generating")
    if status in {VerificationTaskStatus.PENDING, VerificationTaskStatus.QUEUED}:
        return ("pending", "pending", "")
    if status == VerificationTaskStatus.FAILED:
        return ("fail", "failed", "failed")
    if status == VerificationTaskStatus.CANCELLED:
        return ("neutral", "cancelled", "cancelled")
    if status == VerificationTaskStatus.DONE and verdict.upper() == "SK":
        return ("warn", "skipped", "skipped")
    if status == VerificationTaskStatus.DONE:
        return ("ok", "ok", "ready")
    return ("pending", "pending", "")


def build_test_generation_views(
    task_rows: list[VerificationTaskRow],
    tests_meta_by_test_name: dict[str, dict[str, object]],
    *,
    limit_bytes: int,
) -> dict[str, TestGenerationView]:
    generate_rows = [
        row
        for row in task_rows
        if str(row["task_kind"]) == "generate-input" and str(row["test_name"])
    ]
    generate_row_by_test_name = {str(row["test_name"]): row for row in generate_rows}
    duplicate_owner_by_task_id = _duplicate_owner_by_task_id(generate_rows)
    test_names = set(tests_meta_by_test_name) | set(generate_row_by_test_name)
    views: dict[str, TestGenerationView] = {}
    terminal_statuses = {
        VerificationTaskStatus.DONE,
        VerificationTaskStatus.FAILED,
        VerificationTaskStatus.CANCELLED,
    }
    for test_name in test_names:
        row = generate_row_by_test_name.get(test_name)
        tests_meta = tests_meta_by_test_name.get(test_name, {})
        source_kind, command, source_path = _source_kind_and_command(row, tests_meta)
        status = "" if row is None else str(row["status"])
        verdict = "" if row is None else str(row["verdict"])
        tone, status_label, table_text = _status_presentation(status, verdict)
        detail = ""
        error_text = ""
        feedback_text = ""
        duplicate_of = ""
        skipped = bool(status == VerificationTaskStatus.DONE and verdict.upper() == "SK")
        alert_severity = ""
        alert_message = ""
        if row is not None:
            error_text = bounded_display_text(
                str(row["error_text"] or ""),
                limit_bytes=limit_bytes,
            )
            feedback_text = bounded_display_text(
                str(row["feedback_text"] or ""),
                limit_bytes=limit_bytes,
            )
            late_diagnostic_text = bounded_display_text(
                str(row.get("late_diagnostic_text") or ""),
                limit_bytes=limit_bytes,
            )
            if late_diagnostic_text:
                if error_text:
                    if late_diagnostic_text not in error_text:
                        error_text = bounded_display_text(
                            f"{error_text}\n\n{late_diagnostic_text}",
                            limit_bytes=limit_bytes,
                        )
                elif late_diagnostic_text not in feedback_text:
                    feedback_text = bounded_display_text(
                        "\n\n".join(
                            value
                            for value in (feedback_text, late_diagnostic_text)
                            if value
                        ),
                        limit_bytes=limit_bytes,
                    )
            detail = error_text or feedback_text
            duplicate_of = duplicate_owner_by_task_id.get(str(row["id"]), "")
        if skipped:
            alert_severity = "warning"
            alert_message = (
                f"duplicate of {duplicate_of}"
                if duplicate_of
                else "skipped (duplicate owner unavailable)"
            )
            table_text = alert_message
        elif status == VerificationTaskStatus.CANCELLED:
            alert_severity = "warning"
            alert_message = detail or "generation cancelled"
        elif status == VerificationTaskStatus.FAILED:
            alert_severity = "error"
            alert_message = detail or "generation failed"
        views[test_name] = {
            "source_kind": source_kind,
            "command": command,
            "source_path": source_path,
            "display_source": "manual validation" if source_kind == "manual" else source_path,
            "status": status,
            "verdict": verdict,
            "status_text": generation_status_text(status, verdict),
            "feedback_display": feedback_text or "-",
            "error_text": error_text,
            "terminal": status in terminal_statuses,
            "tone": tone,
            "status_label": status_label,
            "table_text": table_text,
            "detail": detail,
            "alert_severity": alert_severity,
            "alert_message": alert_message,
            "duplicate_of": duplicate_of,
            "skipped": skipped,
        }
    return views


def generation_warning_message(
    views_by_test_name: dict[str, TestGenerationView],
    ordered_test_names: list[str],
) -> str:
    entries = [
        f"{test_name} {views_by_test_name[test_name]['alert_message']}"
        for test_name in ordered_test_names
        if test_name in views_by_test_name and views_by_test_name[test_name]["skipped"]
    ]
    return "; ".join(entries)
