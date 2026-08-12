from __future__ import annotations

from typing import Annotated

import app.main_constant as _K
from app.impl.auth.session import require_session_user

from fastapi import Form, Request, Depends

from app.impl.auth.shared import enforce_same_origin_state_change, login_redirect, redirect_response
from app.impl.auth.session import session_user
from app.impl.runtime.config import config
from app.impl.problem.shared import _has_destructive_sudo_for_ctx, _sudo_redirect_for_destructive
from app.impl.workspace.context_operation import normalize_page_target
from app.impl.workspace.access import require_manage_access, require_write_access, workspace_access_context
from app.impl.workspace.context_ui import page_ctx
from app.main_util import problem_slug_leaf

_C = config.config_values


def switch_workspace(
    request: Request,
    problem: str = Form(...),
    page: str = Form("statement"),
):
    active_user = session_user(request)
    if not active_user:
        return login_redirect(request)
    raw_problem = problem.strip()
    try:
        if not raw_problem:
            raise ValueError('problem id is required')
        user_id = config.workspace_service.known_user_id(active_user)
        if user_id is None:
            ensured = config.workspace_service.ensure_user(active_user)
            user_id = int(ensured["id"])
        problem_owner = active_user.lower()
        if "/" in raw_problem:
            safe_problem = raw_problem
        else:
            owned_problem = f"{problem_owner}/{raw_problem}"
            owned_problem_id = config.workspace_service.known_problem_id(owned_problem)
            if owned_problem_id is not None:
                safe_problem = owned_problem
            else:
                global_leaf_matches = config.workspace_service.problem_slugs_by_leaf(raw_problem, limit=20)
                accessible_leaf_matches = config.workspace_service.accessible_problem_slugs_by_leaf(user_id or 0, raw_problem, limit=20)
                foreign_accessible_matches = [slug for slug in accessible_leaf_matches if slug != owned_problem]
                if len(global_leaf_matches) == 1 and len(foreign_accessible_matches) == 1:
                    safe_problem = foreign_accessible_matches[0]
                elif global_leaf_matches:
                    raise ValueError(
                        f"problem slug '{raw_problem}' already exists under another owner; use the full problem id to open it "
                        f"or enter {owned_problem} explicitly to create your own copy"
                    )
                else:
                    safe_problem = owned_problem
        if len(safe_problem) > _K.PROBLEM_ID_MAX_LEN or not _K.PROBLEM_IDENT_RE.fullmatch(safe_problem):
            raise ValueError(_K.PROBLEM_ID_RULE_MESSAGE)
        problem_id = config.workspace_service.known_problem_id(safe_problem)
        if problem_id is None:
            config.workspace_service.ensure_problem(safe_problem)
            config.workspace_service.grant_repo_access(safe_problem, active_user, 'owner')
        else:
            access = workspace_access_context(problem_id, user_id)
            if not bool(access.get('can_read')):
                raise ValueError('you do not have access to this problem; ask an owner to grant access')
        config.workspace_service.ensure_workspace(safe_problem, active_user)
    except ValueError as exc:
        msg = str(exc)
        return redirect_response('/problems', status_code=303, message=msg)
    target_page = normalize_page_target(page)
    return redirect_response(f'/problems/{safe_problem}/{target_page}', status_code=303)

def workspace_delete(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    enforce_same_origin_state_change(request)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    next_path = f'/problems/{problem}/workspace'
    if not _has_destructive_sudo_for_ctx(request, ctx):
        return _sudo_redirect_for_destructive(next_path)
    msg = 'workspace files deleted; they will be recreated on next open'
    try:
        config.workspace_service.delete_workspace(problem, user)
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
        return redirect_response(next_path, status_code=303, message=msg)
    except Exception as exc:
        msg = f"workspace delete failed: {exc}"
        return redirect_response(next_path, status_code=303, message=msg)
    return redirect_response("/problems", status_code=303, message=msg)

def problem_delete(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)], confirm_problem: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    next_path = f'/problems/{problem}/workspace'
    if not _has_destructive_sudo_for_ctx(request, ctx):
        return _sudo_redirect_for_destructive(next_path)
    msg = 'problem deleted'
    try:
        expected_slug = str(ctx['problem']['slug'] or "").strip()
        expected = problem_slug_leaf(expected_slug)
        if problem_slug_leaf(confirm_problem) != expected:
            raise ValueError('problem deletion confirmation mismatch')
        result = config.workspace_service.delete_problem(problem)
        warnings = result.get('fs_warnings') if isinstance(result, dict) else []
        warning_rows = [item.strip() for item in warnings if isinstance(item, str) and item.strip()]
        if warning_rows:
            msg = f"problem deleted with cleanup warnings: {warning_rows[0]}"
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
        return redirect_response(next_path, status_code=303, message=msg)
    except Exception as exc:
        msg = f"problem delete failed: {exc}"
        return redirect_response(next_path, status_code=303, message=msg)
    return redirect_response("/problems", status_code=303, message=msg)
