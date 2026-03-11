from __future__ import annotations

from fastapi import Form, Request

from app.impl.auth.public import login_redirect, redirect_response, session_user
from app.impl.runtime.config import config
from app.impl.problem.shared import _has_destructive_sudo_for_ctx, _sudo_redirect_for_destructive
from app.impl.workspace.public import (
    audit,
    form_text,
    normalize_page_target,
    require_manage_access,
    require_write_access,
    workspace_access_context,
    page_ctx,
)

_C = config.constants


def switch_workspace(
    request: Request,
    problem: str = Form(...),
    user: str = Form(""),
    page: str = Form("statement"),
    problem_name: str = Form(""),
):
    active_user = session_user(request) or str(user or '').strip()
    if not active_user:
        return login_redirect(request)
    raw_problem = str(problem or '').strip()
    try:
        if not raw_problem:
            raise ValueError('problem id is required')
        if "/" in raw_problem:
            safe_problem = raw_problem
        else:
            safe_problem = f"{active_user}/{raw_problem}"
        if not _C.PROBLEM_IDENT_RE.fullmatch(safe_problem):
            raise ValueError(_C.PROBLEM_ID_RULE_MESSAGE)
        user_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [active_user])
        if user_row is None:
            ensured = config.workspace_service.ensure_user(active_user)
            user_id = int(ensured['id'])
        else:
            user_id = int(user_row['id'])
        problem_row = config.db.fetch_one('SELECT id FROM problems WHERE slug=?', [safe_problem])
        if problem_row is None:
            requested_name = form_text(problem_name).strip()
            if not requested_name:
                requested_name = f'{safe_problem.title()} Problem'
            config.workspace_service.ensure_problem(safe_problem, requested_name)
            config.workspace_service.grant_repo_access(safe_problem, active_user, 'owner')
        else:
            access = workspace_access_context(int(problem_row['id']), user_id)
            if not bool(access.get('can_read')):
                raise ValueError('you do not have access to this problem; ask an owner to grant access')
        config.workspace_service.ensure_workspace(safe_problem, active_user)
    except ValueError as exc:
        msg = str(exc)
        return redirect_response('/problems', status_code=303, message=msg)
    target_page = normalize_page_target(page)
    return redirect_response(f'/problems/{safe_problem}/{active_user}/{target_page}', status_code=303)

def workspace_delete(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    next_path = f'/problems/{problem}/{user}/workspace'
    if not _has_destructive_sudo_for_ctx(request, ctx):
        return _sudo_redirect_for_destructive(next_path)
    msg = 'working copy deleted; it will be recreated on next open'
    try:
        result = config.workspace_service.delete_workspace(problem, user)
        audit(
            int(ctx['user']['id']),
            int(ctx['problem']['id']),
            'workspace.delete',
            {'workspace_path': str(result.get('workspace_path') or ''), 'removed': bool(result.get('removed'))},
        )
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
        return redirect_response(next_path, status_code=303, message=msg)
    except Exception as exc:
        msg = f"workspace delete failed: {exc}"
        return redirect_response(next_path, status_code=303, message=msg)
    return redirect_response("/problems", status_code=303, message=msg)

def problem_delete(request: Request, problem: str, user: str, confirm_problem: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    next_path = f'/problems/{problem}/{user}/workspace'
    if not _has_destructive_sudo_for_ctx(request, ctx):
        return _sudo_redirect_for_destructive(next_path)
    msg = 'problem deleted'
    try:
        expected = str(ctx['problem'].get('slug') or '').strip()
        if form_text(confirm_problem).strip() != expected:
            raise ValueError('problem deletion confirmation mismatch')
        result = config.workspace_service.delete_problem(problem)
        warnings = result.get('fs_warnings') if isinstance(result, dict) else []
        warning_rows = [str(item).strip() for item in (warnings or []) if str(item or '').strip()]
        audit(
            int(ctx['user']['id']),
            None,
            'problem.delete',
            {
                'problem_slug': expected,
                'problem_id': int(ctx['problem']['id']),
                'workspace_count': int(result.get('workspace_count') or 0) if isinstance(result, dict) else 0,
                'fs_warnings': warning_rows,
            },
        )
        if warning_rows:
            msg = f"problem deleted with cleanup warnings: {warning_rows[0]}"
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
        return redirect_response(next_path, status_code=303, message=msg)
    except Exception as exc:
        msg = f"problem delete failed: {exc}"
        return redirect_response(next_path, status_code=303, message=msg)
    return redirect_response("/problems", status_code=303, message=msg)



