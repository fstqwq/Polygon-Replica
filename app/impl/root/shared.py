from __future__ import annotations

from fastapi import HTTPException, Request

from app.impl.auth.session import session_user


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"


def _active_root_user(request: Request | None = None, user: str = "") -> str:
    explicit = str(user or "").strip()
    if explicit:
        return explicit
    if request is None:
        raise HTTPException(status_code=400, detail="missing user context")
    active_user = str(session_user(request) or "").strip()
    if not active_user:
        raise HTTPException(status_code=401, detail="authentication required")
    return active_user
