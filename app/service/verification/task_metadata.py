from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypedDict, cast

from app.service.platform.error_text import truncate_display_text


class TruncatedText(TypedDict):
    text: str
    truncated: bool
    total_bytes: int


class CanonicalDiagnostics(TypedDict):
    rows: list[dict[str, object]]
    truncated: bool
    total: int


def canonical_truncated_text(value: str, *, limit: int) -> TruncatedText:
    raw_text = str(value or "")
    text, truncated = truncate_display_text(raw_text, limit_bytes=max(1, int(limit)))
    return {
        "text": text,
        "truncated": bool(truncated),
        "total_bytes": len(raw_text.encode("utf-8")),
    }


def _normalize_diagnostics_for_db(
    entries: list[Mapping[str, object]] | list[object],
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
    entries: list[Mapping[str, object]] | list[object] | None,
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
        "rows": cast(list[dict[str, object]], rows),
        "truncated": total > len(rows),
        "total": total,
    }


def diagnostics_json_text(rows: list[dict[str, object]]) -> str:
    return json.dumps(rows, ensure_ascii=True, separators=(",", ":"))


def normalize_diagnostics_json_text(raw_json: str, *, message_limit: int) -> str:
    text = str(raw_json or "").strip()
    if not text:
        return "[]"
    try:
        payload = json.loads(text)
    except Exception:
        return "[]"
    if not isinstance(payload, list):
        return "[]"
    if not payload:
        return "[]"
    rows = _normalize_diagnostics_for_db(payload, max(1, int(message_limit)))
    return diagnostics_json_text(rows)
