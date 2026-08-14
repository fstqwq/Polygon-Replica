from typing import Annotated

from app.impl.auth.session import require_session_user

from fastapi import Form, Depends

from app.impl.auth.shared import normalize_username_required, redirect_response
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import require_manage_access
from app.service.access.policy import transferable_repo_role
from app.impl.workspace.context_ui import page_ctx


def workspace_access_grant(problem: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...), role: str = Form("read")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = "access updated"
    try:
        safe_target = normalize_username_required(target_user)
        safe_role = transferable_repo_role(role)
        problem_id = int(ctx["problem"]["id"])
        runtime().workspace_service.set_repo_access_for_problem_id(problem_id, safe_target, safe_role)
        msg = f"access updated: {safe_target} -> {safe_role}"
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f"/problems/{problem}/access", status_code=303, message=msg)


def workspace_access_revoke(problem: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = "access removed"
    redirect_to_problems = False
    try:
        safe_target = normalize_username_required(target_user)
        problem_id = int(ctx["problem"]["id"])
        result = runtime().workspace_service.revoke_repo_access_for_problem_id(problem_id, safe_target)
        target_user_id = result["target_user_id"]
        if not isinstance(target_user_id, int) or isinstance(target_user_id, bool):
            raise RuntimeError("revoked access user id must be an integer")
        redirect_to_problems = target_user_id == int(ctx["user"]["id"])
        msg = f"access removed: {safe_target}"
    except ValueError as exc:
        msg = str(exc)
    if redirect_to_problems:
        return redirect_response("/problems", status_code=303, message=msg)
    return redirect_response(f"/problems/{problem}/access", status_code=303, message=msg)
