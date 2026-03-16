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
    row = config.db.fetch_one("SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?", [problem_id, user_id])
    if row is None:
        return {
            "role": "none",
            "can_read": False,
            "can_write": False,
            "can_manage": False,
            "read_block_reason": "you do not have access to this problem",
            "write_block_reason": "write access required",
            "manage_block_reason": "owner access required",
        }
    role = row["role"]
    if role not in {"owner", "write", "read"}:
        raise RuntimeError("invalid repo role")
    can_write = role in {"owner", "write"}
    return {
        "role": role,
        "can_read": True,
        "can_write": can_write,
        "can_manage": role == "owner",
        "read_block_reason": "",
        "write_block_reason": "" if can_write else "read-only access",
        "manage_block_reason": "" if role == "owner" else "owner access required",
    }


def normalize_repo_role(raw: str) -> str:
    role = raw.strip().lower()
    if role in {"owner", "write", "read"}:
        return role
    raise ValueError("invalid role")


def problem_owner_count(problem_id: int) -> int:
    row = config.db.fetch_one(
        "SELECT COUNT(*) AS c FROM repo_acl WHERE problem_id=? AND role='owner'",
        [problem_id],
    )
    if row is None:
        return 0
    return max(0, row["c"])


def problem_acl_entries(problem_id: int) -> list[ProblemAclEntry]:
    rows = config.db.fetch_all(
        """
        SELECT u.username,a.role,a.created_at
        FROM repo_acl a
        JOIN users u ON u.id=a.user_id
        WHERE a.problem_id=?
        ORDER BY
            CASE a.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
            u.username ASC
        """,
        [problem_id],
    )
    entries: list[ProblemAclEntry] = []
    for row in rows:
        role = row["role"]
        if role not in {"owner", "write", "read"}:
            raise RuntimeError("invalid repo role")
        entries.append({"username": row["username"], "role": role, "created_at": row["created_at"]})
    return entries


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
    row = config.db.fetch_one("SELECT is_system_admin FROM users WHERE id=?", [user_id])
    if row is None:
        return False
    return row["is_system_admin"] == 1


def require_system_admin(ctx: PageContext) -> None:
    user_row = ctx["user"]
    user_id = user_row["id"]
    if is_system_admin_user_id(user_id):
        user_row["is_system_admin"] = 1
        return
    raise HTTPException(status_code=403, detail="system admin required")
