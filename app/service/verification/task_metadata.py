from __future__ import annotations

import json
from typing import TypedDict, cast

from app.service.run.summary import normalize_diagnostics_for_db, truncate_inline_text


class TruncatedText(TypedDict):
    text: str
    truncated: bool
    total_chars: int


class CanonicalDiagnostics(TypedDict):
    rows: list[dict[str, object]]
    truncated: bool
    total: int


def canonical_truncated_text(value: str, *, limit: int) -> TruncatedText:
    raw_text = value or ""
    text, truncated = truncate_inline_text(raw_text, max_chars=max(1, int(limit)))
    return {
        "text": text,
        "truncated": bool(truncated),
        "total_chars": len(raw_text),
    }


def canonical_diagnostics(
    entries: list[dict[str, object]] | list[object] | None,
    *,
    list_limit: int,
    message_limit: int,
) -> CanonicalDiagnostics:
    raw_rows = list(entries or [])
    total = len(raw_rows)
    cap = max(1, int(list_limit))
    selected = raw_rows[:cap]
    rows = normalize_diagnostics_for_db(selected, max(1, int(message_limit)))
    return {
        "rows": cast(list[dict[str, object]], rows),
        "truncated": total > len(rows),
        "total": total,
    }


def diagnostics_json_text(rows: list[dict[str, object]]) -> str:
    return json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
