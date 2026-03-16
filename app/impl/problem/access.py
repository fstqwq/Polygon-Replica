from __future__ import annotations

from fastapi import Form

from app.impl.auth.shared import normalize_username_required, redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.impl.workspace.access import normalize_repo_role, require_manage_access
from app.impl.workspace.context_job import page_ctx


def workspace_access_grant(problem: str, user: str, target_user: str = Form(...), role: str = Form("read")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = "access updated"
    try:
        safe_target = normalize_username_required(target_user)
        safe_role = normalize_repo_role(role)
        problem_id = int(ctx["problem"]["id"])
        config.workspace_service.set_repo_access_for_problem_id(problem_id, safe_target, safe_role)
        audit(int(ctx["user"]["id"]), problem_id, "access.grant", {"target_user": safe_target, "role": safe_role})
        msg = f"access updated: {safe_target} -> {safe_role}"
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f"/problems/{problem}/{user}/access", status_code=303, message=msg)


def workspace_access_revoke(problem: str, user: str, target_user: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = "access removed"
    redirect_to_problems = False
    try:
        safe_target = normalize_username_required(target_user)
        problem_id = int(ctx["problem"]["id"])
        result = config.workspace_service.revoke_repo_access_for_problem_id(problem_id, safe_target)
        redirect_to_problems = int(result["target_user_id"]) == int(ctx["user"]["id"])
        audit(int(ctx["user"]["id"]), problem_id, "access.revoke", {"target_user": safe_target})
        msg = f"access removed: {safe_target}"
    except ValueError as exc:
        msg = str(exc)
    if redirect_to_problems:
        return redirect_response("/problems", status_code=303, message=msg)
    return redirect_response(f"/problems/{problem}/{user}/access", status_code=303, message=msg)
