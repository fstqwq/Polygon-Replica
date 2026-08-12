from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal, cast

from app.service.platform.error_text import (
    bounded_display_text,
)
from app.service.platform.hashing import canonical_json, sha256_hex_json
from app.service.execution.model import ExecutionResult


DiagnosticKind = Literal["debug-info", "internal-error"]
DiagnosticMergeOutcome = Literal["persisted", "duplicate", "not-applicable"]
DIAGNOSTIC_KINDS = frozenset(("debug-info", "internal-error"))


@dataclass(frozen=True)
class TaskDiagnosticItem:
    kind: DiagnosticKind
    hostname: str
    text: str
    received_at: str
    digest: str


@dataclass(frozen=True)
class TaskDiagnosticSnapshot:
    items: tuple[TaskDiagnosticItem, ...] = ()


def task_diagnostic_snapshot_json(snapshot: TaskDiagnosticSnapshot) -> str:
    return canonical_json(
        {"items": [asdict(item) for item in snapshot.items]},
        ensure_ascii=False,
    )


def task_diagnostic_snapshot_from_json(raw: str) -> TaskDiagnosticSnapshot:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return TaskDiagnosticSnapshot()
    if not isinstance(payload, dict):
        return TaskDiagnosticSnapshot()
    values: list[TaskDiagnosticItem] = []
    for raw_item in cast(list[object], payload.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        kind = str(raw_item.get("kind") or "")
        if kind not in DIAGNOSTIC_KINDS:
            continue
        values.append(
            TaskDiagnosticItem(
                kind=cast(DiagnosticKind, kind),
                hostname=str(raw_item.get("hostname") or ""),
                text=str(raw_item.get("text") or ""),
                received_at=str(raw_item.get("received_at") or ""),
                digest=str(raw_item.get("digest") or ""),
            )
        )
    return TaskDiagnosticSnapshot(items=tuple(values))


def new_task_diagnostic_item(
    *,
    kind: str,
    hostname: str,
    text: str,
    received_at: str,
    limit_bytes: int,
) -> TaskDiagnosticItem:
    if kind not in DIAGNOSTIC_KINDS:
        raise ValueError(f"unknown verification diagnostic kind: {kind}")
    if not hostname or len(hostname) > 255:
        raise ValueError("verification diagnostic hostname is invalid")
    if not received_at or len(received_at) > 64:
        raise ValueError("verification diagnostic received-at timestamp is invalid")
    normalized_text = bounded_display_text(text, limit_bytes=max(1, limit_bytes))
    if not normalized_text:
        raise ValueError("verification diagnostic text is required")
    digest = sha256_hex_json(
        {"kind": kind, "hostname": hostname, "text": normalized_text},
        ensure_ascii=False,
    )
    return TaskDiagnosticItem(
        kind=cast(DiagnosticKind, kind),
        hostname=hostname,
        text=normalized_text,
        received_at=received_at,
        digest=digest,
    )


def merge_task_diagnostic_snapshot(
    snapshot: TaskDiagnosticSnapshot,
    item: TaskDiagnosticItem,
    *,
    limit_bytes: int,
) -> tuple[TaskDiagnosticSnapshot, DiagnosticMergeOutcome]:
    if any(existing.digest == item.digest for existing in snapshot.items):
        return (snapshot, "duplicate")
    limit = max(1, int(limit_bytes))
    retained = [*snapshot.items, item]
    candidate = TaskDiagnosticSnapshot(items=tuple(retained))
    while len(retained) > 1 and len(
        task_diagnostic_snapshot_json(candidate).encode("utf-8")
    ) > limit:
        retained.pop(0)
        candidate = TaskDiagnosticSnapshot(items=tuple(retained))
    encoded = task_diagnostic_snapshot_json(candidate).encode("utf-8")
    if len(encoded) <= limit:
        return (candidate, "persisted")

    bounded = _fit_single_diagnostic_item(retained[-1], limit_bytes=limit)
    if bounded is None:
        # The required identity metadata and even the truncation marker do not
        # fit. Preserve the prior snapshot instead of persisting an oversized
        # or content-free diagnostic row.
        return (snapshot, "not-applicable")
    return (TaskDiagnosticSnapshot(items=(bounded,)), "persisted")


def _fit_single_diagnostic_item(
    item: TaskDiagnosticItem,
    *,
    limit_bytes: int,
) -> TaskDiagnosticItem | None:
    """Return the longest marked prefix whose serialized snapshot fits exactly."""

    def _candidate(prefix_chars: int) -> TaskDiagnosticItem:
        prefix = item.text[:prefix_chars].rstrip()
        text = f"{prefix}..." if prefix else "..."
        return TaskDiagnosticItem(
            kind=item.kind,
            hostname=item.hostname,
            text=text,
            received_at=item.received_at,
            digest=item.digest,
        )

    def _fits(candidate: TaskDiagnosticItem) -> bool:
        payload = task_diagnostic_snapshot_json(
            TaskDiagnosticSnapshot(items=(candidate,))
        )
        return len(payload.encode("utf-8")) <= limit_bytes

    shortest = _candidate(0)
    if not _fits(shortest):
        return None

    low = 0
    high = max(0, len(item.text) - 1)
    best = shortest
    while low <= high:
        middle = (low + high) // 2
        candidate = _candidate(middle)
        if _fits(candidate):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def compose_task_diagnostic_display(
    result: ExecutionResult,
    snapshot: TaskDiagnosticSnapshot,
    *,
    limit_bytes: int,
) -> dict[str, object]:
    """Build display-only diagnostics without changing the canonical result."""

    late_text = bounded_display_text(
        "\n\n".join(
            (
                f"[late {item.kind} from {item.hostname} at {item.received_at}]\n"
                f"{item.text}"
            )
            for item in snapshot.items
        ),
        limit_bytes=limit_bytes,
    )
    return {
        "canonical_error": result.outcome.error,
        "canonical_feedback": result.outcome.feedback,
        "late_diagnostics": [asdict(item) for item in snapshot.items],
        "late_text": late_text,
    }


def truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or "")
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"... [truncated; showing first {cap} characters]", True


def normalize_diagnostics_for_db(entries: list[dict], message_limit: int) -> list[dict]:
    normalized: list[dict] = []
    cap = max(1, int(message_limit))
    for item in entries:
        message = item.get("message") or ""
        msg, msg_truncated = truncate_inline_text(message, cap)
        row = dict(item)
        row["message"] = msg
        row["message_truncated"] = bool(msg_truncated)
        row["message_limit"] = cap
        normalized.append(row)
    return normalized
