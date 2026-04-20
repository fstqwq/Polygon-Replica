from __future__ import annotations

SANITY_ATTENTION_STATUSES = frozenset({"warning", "failed"})


def normalized_sanity_status(raw: object) -> str:
    status = str(raw or "").strip().lower()
    if status in {"passed", "pending", "running", "warning", "failed", "skipped"}:
        return status
    return "unknown"


def sanity_status_attention(status: str) -> bool:
    return normalized_sanity_status(status) in SANITY_ATTENTION_STATUSES


def sanity_status_display(status: str) -> str:
    token = normalized_sanity_status(status)
    if token in SANITY_ATTENTION_STATUSES:
        return f"Sanity {token}"
    return ""


def verification_status_display(status: str, sanity_status: str) -> str:
    status_token = str(status or "")
    sanity_display = sanity_status_display(sanity_status)
    if status_token in {"ok", "pass"} and sanity_display:
        return f"ok ({sanity_display})"
    return status_token
