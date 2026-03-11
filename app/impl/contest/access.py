from __future__ import annotations

from fastapi import Form, HTTPException, Request

from app.db import now_iso
from app.impl.auth.public import template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import audit, normalize_contest_role

from .common import _normalize_contest_member_role_required
from .shared import _contest_ctx, _contest_owner_count, _contest_redirect
def contest_access_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "access")
    contest_id = int(ctx["contest"]["id"])
    rows = config.db.fetch_all(
        """
        SELECT u.username,m.role,m.created_at
        FROM contest_members m
        JOIN users u ON u.id=m.user_id
        WHERE m.contest_id=?
        ORDER BY
            CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
            u.username ASC
        """,
        [contest_id],
    )
    entries: list[dict[str, object]] = []
    for row in rows:
        entries.append(
            {
                "username": str(row["username"]),
                "role": normalize_contest_role(row["role"]),
                "created_at": row["created_at"],
            }
        )
    return template_response(
        request,
        "contest_access.html",
        {
            "ctx": ctx,
            "entries": entries,
            "owner_count": _contest_owner_count(contest_id),
        },
    )

def contest_access_grant(contest: str, user: str, target_user: str = Form(...), role: str = Form("read")):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("manage_block_reason") or "owner access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = str(target_user or "").strip()
    safe_role = _normalize_contest_member_role_required(role)
    user_row = config.db.fetch_one("SELECT id FROM users WHERE username=?", [safe_target])
    if user_row is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"user {safe_target} not found; ask them to register first")
    target_user_id = int(user_row["id"])
    config.db.execute(
        """
        INSERT INTO contest_members(contest_id,user_id,role,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(contest_id,user_id) DO UPDATE SET role=excluded.role
        """,
        [contest_id, target_user_id, safe_role, now_iso()],
    )
    audit(
        actor_user_id,
        None,
        "contest.access.grant",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
            "role": safe_role,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"granted {safe_role} to {safe_target}")

def contest_access_revoke(contest: str, user: str, target_user: str = Form(...)):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("manage_block_reason") or "owner access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = str(target_user or "").strip()
    target_row = config.db.fetch_one(
        """
        SELECT m.role,u.id AS user_id
        FROM contest_members m
        JOIN users u ON u.id=m.user_id
        WHERE m.contest_id=? AND u.username=?
        """,
        [contest_id, safe_target],
    )
    if target_row is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"{safe_target} is not a member")
    target_role = normalize_contest_role(target_row["role"])
    if target_role == "owner" and _contest_owner_count(contest_id) <= 1:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message="cannot remove the last owner")
    config.db.execute(
        """
        DELETE FROM contest_members
        WHERE contest_id=?
          AND user_id=?
        """,
        [contest_id, int(target_row["user_id"])],
    )
    audit(
        actor_user_id,
        None,
        "contest.access.revoke",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "access", message=f"revoked access for {safe_target}")



