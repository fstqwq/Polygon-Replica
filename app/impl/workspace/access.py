from collections.abc import Mapping

from fastapi import HTTPException

from app.impl.runtime.dependency import runtime
from app.service.access.model import ProblemAccessContext, ProblemAclEntry

def workspace_access_context(problem_id: int, user_id: int) -> ProblemAccessContext:
    return runtime().access_query.problem_context(problem_id, user_id)

def problem_owner_count(problem_id: int) -> int:
    return runtime().workspace_service.owner_count(problem_id)


def problem_acl_entries(problem_id: int) -> list[ProblemAclEntry]:
    return runtime().workspace_service.access_entries(problem_id)


def _context_row(ctx: Mapping[str, object], key: str) -> dict[str, object]:
    value = ctx.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"page context {key} must be an object")
    return value


def require_read_access(ctx: Mapping[str, object]) -> None:
    access = _context_row(ctx, "access")
    if bool(access.get("can_read")):
        return
    raise HTTPException(
        status_code=403,
        detail=str(access.get("read_block_reason") or "read access denied"),
    )


def require_write_access(ctx: Mapping[str, object]) -> None:
    access = _context_row(ctx, "access")
    if bool(access.get("can_write")):
        return
    raise HTTPException(
        status_code=403,
        detail=str(access.get("write_block_reason") or "write access denied"),
    )


def require_manage_access(ctx: Mapping[str, object]) -> None:
    access = _context_row(ctx, "access")
    if bool(access.get("can_manage")):
        return
    raise HTTPException(
        status_code=403,
        detail=str(access.get("manage_block_reason") or "manage access denied"),
    )


def is_system_admin_user_id(user_id: int) -> bool:
    if user_id <= 0:
        return False
    return runtime().access_query.is_system_admin(user_id)


def require_system_admin(ctx: Mapping[str, object]) -> None:
    user_row = _context_row(ctx, "user")
    user_id = user_row.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise RuntimeError("page context user id must be an integer")
    if is_system_admin_user_id(user_id):
        user_row["is_system_admin"] = 1
        return
    raise HTTPException(status_code=403, detail="system admin required")
