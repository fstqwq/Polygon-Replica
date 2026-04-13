from __future__ import annotations
from app.impl.auth.session import require_session_user
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit

from .common import _normalize_transferable_contest_member_role_required
from .shared import _contest_ctx, _contest_redirect


def contest_access_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "access")
    contest_id = int(ctx["contest"]["id"])
    return template_response(
        request,
        "contest_access.html",
        {
            "ctx": ctx,
            "entries": config.contest_service.member_entries(contest_id),
            "owner_count": config.contest_service.owner_count(contest_id),
        },
    )


def contest_access_grant(contest: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...), role: str = Form("read")):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = target_user.strip()
    try:
        safe_role = _normalize_transferable_contest_member_role_required(role)
        if not config.contest_service.grant_member_role(contest_id, safe_target, safe_role):
            return _contest_redirect(
                str(ctx["contest"]["slug"]),
                "access",
                message=f"user {safe_target} not found; ask them to register first",
            )
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=str(exc))
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
    return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=f"granted {safe_role} to {safe_target}")


def contest_access_revoke(contest: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...)):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = target_user.strip()
    membership = config.contest_service.membership_for_username(contest_id, safe_target)
    if membership is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=f"{safe_target} is not a member")
    if membership["role"] == "owner":
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message="owner access is fixed and cannot be transferred")
    config.contest_service.revoke_member(contest_id, membership["user_id"])
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
    return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=f"revoked access for {safe_target}")
