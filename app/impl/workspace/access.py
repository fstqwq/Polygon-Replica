from __future__ import annotations

from typing import TypedDict

from fastapi import HTTPException

from app.impl.runtime.config import config


WorkspaceAccessContext = TypedDict(
    "WorkspaceAccessContext",
    {
        "role": str,
        "can_read": bool,
        "can_write": bool,
        "can_manage": bool,
        "read_block_reason": str,
        "write_block_reason": str,
        "manage_block_reason": str,
    },
)

ProblemAclEntry = TypedDict(
    "ProblemAclEntry",
    {
        "username": str,
        "role": str,
        "created_at": str,
    },
)

UserContext = TypedDict(
    "UserContext",
    {
        "id": int,
        "is_system_admin": int,
    },
    total=False,
)

PageContext = TypedDict(
    "PageContext",
    {
        "access": WorkspaceAccessContext,
        "user": UserContext,
    },
)


def workspace_access_context(problem_id: int, user_id: int) -> WorkspaceAccessContext:
    return config.workspace_service.access_context(problem_id, user_id)

def normalize_transferable_repo_role(raw: str) -> str:
    role = raw.strip().lower()
    if role in {"write", "read"}:
        return role
    if role == "owner":
        raise ValueError("owner access is fixed and cannot be transferred")
    raise ValueError("invalid role")


def problem_owner_count(problem_id: int) -> int:
    return config.workspace_service.owner_count(problem_id)


def problem_acl_entries(problem_id: int) -> list[ProblemAclEntry]:
    return config.workspace_service.access_entries(problem_id)


def require_read_access(ctx: PageContext) -> None:
    access = ctx["access"]
    if access["can_read"]:
        return
    raise HTTPException(status_code=403, detail=access["read_block_reason"])


def require_write_access(ctx: PageContext) -> None:
    access = ctx["access"]
    if access["can_write"]:
        return
    raise HTTPException(status_code=403, detail=access["write_block_reason"])


def require_manage_access(ctx: PageContext) -> None:
    access = ctx["access"]
    if access["can_manage"]:
        return
    raise HTTPException(status_code=403, detail=access["manage_block_reason"])


def is_system_admin_user_id(user_id: int) -> bool:
    if user_id <= 0:
        return False
    return config.workspace_service.user_is_system_admin(user_id)


def require_system_admin(ctx: PageContext) -> None:
    user_row = ctx["user"]
    user_id = user_row["id"]
    if is_system_admin_user_id(user_id):
        user_row["is_system_admin"] = 1
        return
    raise HTTPException(status_code=403, detail="system admin required")
