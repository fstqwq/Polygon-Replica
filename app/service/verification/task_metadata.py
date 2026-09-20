from collections.abc import Mapping, Sequence
from typing import TypedDict

from app.service.platform.error_text import truncate_display_text


class CanonicalDiagnostics(TypedDict):
    rows: list[dict[str, object]]
    truncated: bool
    total: int


def _normalize_diagnostics_for_db(
    entries: Sequence[object],
    message_limit: int,
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    cap = max(1, int(message_limit))
    for raw in entries:
        item = dict(raw) if isinstance(raw, Mapping) else {"message": str(raw)}
        message = item.get("message")
        safe_message = message if isinstance(message, str) else str(message or "")
        msg, msg_truncated = truncate_display_text(safe_message, limit_bytes=cap)
        row = dict(item)
        row["message"] = msg
        row["message_truncated"] = bool(msg_truncated)
        row["message_limit"] = cap
        row["level"] = (
            row.get("level")
            if isinstance(row.get("level"), str) and str(row.get("level")).strip()
            else "error"
        )
        row["file"] = row.get("file") if isinstance(row.get("file"), str) else ""
        row["line"] = row["line"] if isinstance(row.get("line"), int) else 0
        row["column"] = row["column"] if isinstance(row.get("column"), int) else 0
        row["can_link"] = bool(row.get("can_link")) if "can_link" in row else False
        normalized.append(row)
    return normalized


def canonical_diagnostics(
    entries: Sequence[object] | None,
    *,
    list_limit: int,
    message_limit: int,
) -> CanonicalDiagnostics:
    raw_rows = list(entries or [])
    total = len(raw_rows)
    cap = max(1, int(list_limit))
    selected = raw_rows[:cap]
    rows = _normalize_diagnostics_for_db(selected, max(1, int(message_limit)))
    return {
        "rows": rows,
        "truncated": total > len(rows),
        "total": total,
    }
