from typing import TypedDict

from fastapi import HTTPException

from app.impl.runtime.dependency import runtime
from app.impl.workspace.context import GlobalUserPageContext
from app.service.access.model import ProblemAccessContext, ProblemAclEntry

def workspace_access_context(problem_id: int, user_id: int) -> ProblemAccessContext:
    return runtime().access_query.problem_context(problem_id, user_id)

def problem_owner_count(problem_id: int) -> int:
    return runtime().workspace_service.owner_count(problem_id)


def problem_acl_entries(problem_id: int) -> list[ProblemAclEntry]:
    return runtime().workspace_service.access_entries(problem_id)


class ProblemAccessPageContext(TypedDict):
    access: ProblemAccessContext


def require_read_access(ctx: ProblemAccessPageContext) -> None:
    access = ctx["access"]
    if access["can_read"]:
        return
    raise HTTPException(
        status_code=403,
        detail=access["read_block_reason"] or "read access denied",
    )


def require_write_access(ctx: ProblemAccessPageContext) -> None:
    access = ctx["access"]
    if access["can_write"]:
        return
    raise HTTPException(
        status_code=403,
        detail=access["write_block_reason"] or "write access denied",
    )


def require_manage_access(ctx: ProblemAccessPageContext) -> None:
    access = ctx["access"]
    if access["can_manage"]:
        return
    raise HTTPException(
        status_code=403,
        detail=access["manage_block_reason"] or "manage access denied",
    )


def require_problem_access_management(ctx: ProblemAccessPageContext) -> None:
    access = ctx["access"]
    if access["can_manage_access"]:
        return
    raise HTTPException(
        status_code=403,
        detail=access["access_manage_block_reason"] or "problem write access required",
    )


def is_system_admin_user_id(user_id: int) -> bool:
    if user_id <= 0:
        return False
    return runtime().access_query.is_system_admin(user_id)


def require_system_admin(ctx: GlobalUserPageContext) -> None:
    user_row = ctx["user"]
    if is_system_admin_user_id(user_row["id"]):
        user_row["is_system_admin"] = 1
        return
    raise HTTPException(status_code=403, detail="system admin required")
