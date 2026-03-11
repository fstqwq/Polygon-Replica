from __future__ import annotations

from fastapi import HTTPException

from app.impl.runtime.config import config


def workspace_access_context(problem_id: int, user_id: int) -> dict:
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
    role = str(row["role"]).strip().lower()
    if role not in {"owner", "write", "read"}:
        role = "read"
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


def normalize_repo_role(raw: object) -> str:
    role = str(raw or "").strip().lower()
    if role in {"owner", "write", "read"}:
        return role
    raise ValueError("invalid role")


def problem_owner_count(problem_id: int) -> int:
    row = config.db.fetch_one(
        "SELECT COUNT(*) AS c FROM repo_acl WHERE problem_id=? AND role='owner'",
        [int(problem_id)],
    )
    if row is None:
        return 0
    try:
        return max(0, int(row["c"] or 0))
    except Exception:
        return 0


def problem_acl_entries(problem_id: int) -> list[dict]:
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
        [int(problem_id)],
    )
    entries: list[dict] = []
    for row in rows:
        role = str(row["role"] or "").strip().lower()
        if role not in {"owner", "write", "read"}:
            role = "read"
        entries.append({"username": str(row["username"]), "role": role, "created_at": row["created_at"]})
    return entries


def require_read_access(ctx: dict) -> None:
    access = ctx.get("access") if isinstance(ctx, dict) else None
    can_read = bool(access.get("can_read")) if isinstance(access, dict) else False
    if can_read:
        return
    reason = "problem access required"
    if isinstance(access, dict):
        reason = str(access.get("read_block_reason") or reason)
    raise HTTPException(status_code=403, detail=reason)


def require_write_access(ctx: dict) -> None:
    access = ctx.get("access") if isinstance(ctx, dict) else None
    can_write = bool(access.get("can_write")) if isinstance(access, dict) else False
    if can_write:
        return
    reason = "write access required"
    if isinstance(access, dict):
        reason = str(access.get("write_block_reason") or reason)
    raise HTTPException(status_code=403, detail=reason)


def require_manage_access(ctx: dict) -> None:
    access = ctx.get("access") if isinstance(ctx, dict) else None
    can_manage = bool(access.get("can_manage")) if isinstance(access, dict) else False
    if can_manage:
        return
    reason = "owner access required"
    if isinstance(access, dict):
        reason = str(access.get("manage_block_reason") or reason)
    raise HTTPException(status_code=403, detail=reason)


def is_system_admin_user_id(user_id: int) -> bool:
    uid = int(user_id)
    if uid <= 0:
        return False
    row = config.db.fetch_one("SELECT is_system_admin FROM users WHERE id=?", [uid])
    if row is None:
        return False
    try:
        return int(row["is_system_admin"] or 0) == 1
    except Exception:
        return False


def require_system_admin(ctx: dict) -> None:
    user_row = ctx.get("user") if isinstance(ctx, dict) else None
    user_id = 0
    if isinstance(user_row, dict):
        try:
            user_id = int(user_row.get("id") or 0)
        except Exception:
            user_id = 0
    if is_system_admin_user_id(user_id):
        if isinstance(user_row, dict):
            user_row["is_system_admin"] = 1
        return
    raise HTTPException(status_code=403, detail="system admin required")


