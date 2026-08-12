from __future__ import annotations

from pathlib import Path

from app.service.verification.task_store import VerificationTaskReadRow
from app.service.verification.types import VerificationTaskStatus


def task_display_status(status: VerificationTaskStatus) -> str:
    if status == VerificationTaskStatus.LEASED:
        return "running"
    return status


def task_counts(rows: list[VerificationTaskReadRow]) -> dict[str, object]:
    counts = {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "by_kind": {},
    }
    by_kind = counts["by_kind"]
    assert isinstance(by_kind, dict)
    for row in rows:
        task_kind = str(row["task_kind"] or "")
        status = task_display_status(row["status"])
        counts["total"] = int(counts["total"]) + 1
        if status in counts:
            counts[status] = int(counts[status]) + 1
        kind_counts = by_kind.get(task_kind)
        if kind_counts is None:
            kind_counts = {
                "pending": 0,
                "queued": 0,
                "running": 0,
                "done": 0,
                "failed": 0,
                "cancelled": 0,
            }
            by_kind[task_kind] = kind_counts
        if status in kind_counts:
            kind_counts[status] = int(kind_counts[status]) + 1
    return counts


def running_tasks(rows: list[VerificationTaskReadRow]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for row in rows:
        if row["status"] != VerificationTaskStatus.LEASED:
            continue
        source_path = str(row["source_path"] or "")
        source_label = Path(source_path).name if source_path else "-"
        test_name = str(row["test_name"] or "")
        task_kind = str(row["task_kind"] or "")
        kind_label = task_kind.replace("-", " ").title()
        values.append(
            {
                "task_id": str(row["id"] or ""),
                "task_kind": task_kind,
                "task_kind_label": kind_label,
                "source_path": source_path,
                "source_label": source_label,
                "test_name": test_name,
                "label": " / ".join(token for token in (kind_label, source_label, test_name) if token),
            }
        )
    return values


def solution_source_paths(rows: list[VerificationTaskReadRow]) -> list[str]:
    values: list[str] = []
    for row in rows:
        if str(row["task_kind"] or "") != "solution-run":
            continue
        source_path = str(row["source_path"] or "")
        if source_path and source_path not in values:
            values.append(source_path)
    return values


def program_ids(rows: list[VerificationTaskReadRow]) -> list[str]:
    values: list[str] = []
    for row in rows:
        program_id = str(row["program_id"] or "")
        if program_id and program_id not in values:
            values.append(program_id)
    return values
