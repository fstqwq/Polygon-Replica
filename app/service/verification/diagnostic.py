from __future__ import annotations

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



