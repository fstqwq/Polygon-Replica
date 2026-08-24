from typing import Annotated

from app.impl.auth.session import require_session_user

from fastapi import Depends, Form, HTTPException

from app.impl.auth.shared import normalize_username_required, redirect_response
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import require_problem_access_management
from app.service.access.policy import transferable_repo_role
from app.impl.workspace.context_ui import page_ctx


def workspace_access_grant(problem: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...), role: str = Form("read")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_problem_access_management(ctx)
    msg = "access updated"
    try:
        safe_target = normalize_username_required(target_user)
        safe_role = transferable_repo_role(role)
        problem_id = int(ctx["problem"]["id"])
        runtime().access_command.set_problem_access(
            actor_user_id=int(ctx["user"]["id"]),
            problem_id=problem_id,
            target_username=safe_target,
            role=safe_role,
        )
        msg = f"access updated: {safe_target} -> {safe_role}"
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f"/problems/{problem}/access", status_code=303, message=msg)


def workspace_access_revoke(problem: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_problem_access_management(ctx)
    msg = "access removed"
    try:
        safe_target = normalize_username_required(target_user)
        problem_id = int(ctx["problem"]["id"])
        runtime().access_command.revoke_problem_access(
            actor_user_id=int(ctx["user"]["id"]),
            problem_id=problem_id,
            target_username=safe_target,
        )
        msg = f"access removed: {safe_target}"
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f"/problems/{problem}/access", status_code=303, message=msg)
