import app.main_constant as _K

from fastapi import HTTPException
from typing import TypedDict

from app.impl.runtime.dependency import runtime
from app.service.repository.workspace import GlobalUserContext


class GlobalUserPageContext(TypedDict):
    user: GlobalUserContext
    default_problem: str



def count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"


def global_user_ctx(username: str) -> GlobalUserPageContext:
    safe_user = str(username or "").strip()
    if not _K.USER_IDENT_RE.fullmatch(safe_user):
        raise HTTPException(status_code=400, detail=_K.USERNAME_RULE_MESSAGE)
    try:
        row = runtime().workspace_service.global_user_context(safe_user)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "user": row,
        "default_problem": runtime().workspace_service.default_problem_slug_for_username(safe_user),
    }
