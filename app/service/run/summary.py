from __future__ import annotations

def truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or "")
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"... [truncated; showing first {cap} characters]", True


def normalize_diagnostics_for_db(entries: list, message_limit: int) -> list[dict]:
    normalized: list[dict] = []
    cap = max(1, int(message_limit))
    for raw in entries:
        item = raw if isinstance(raw, dict) else {"message": str(raw)}
        message = message if isinstance(message := item.get("message"), str) else str(message)
        msg, msg_truncated = truncate_inline_text(message, cap)
        row = dict(item)
        row["message"] = msg
        row["message_truncated"] = bool(msg_truncated)
        row["message_limit"] = cap
        row["level"] = row.get("level") if isinstance(row.get("level"), str) and str(row.get("level")).strip() else "error"
        row["file"] = row.get("file") if isinstance(row.get("file"), str) else ""
        row["line"] = row["line"] if isinstance(row.get("line"), int) else 0
        row["column"] = row["column"] if isinstance(row.get("column"), int) else 0
        row["can_link"] = bool(row.get("can_link")) if "can_link" in row else False
        normalized.append(row)
    return normalized



