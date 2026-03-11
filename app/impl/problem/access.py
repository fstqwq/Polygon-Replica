from __future__ import annotations

from fastapi import Form, Request

from app.db import now_iso
from app.impl.auth.public import normalize_username_required, redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.public import (
    audit,
    normalize_repo_role,
    problem_owner_count,
    render_workspace_page,
    require_manage_access,
    page_ctx,
)


def access_page(request: Request, problem: str, user: str):
    return render_workspace_page(request, problem, user, show_access_admin=True)

def workspace_access_grant(problem: str, user: str, target_user: str=Form(...), role: str=Form('read')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = 'access updated'
    try:
        safe_target = normalize_username_required(target_user)
        safe_role = normalize_repo_role(role)
        target_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [safe_target])
        if target_row is None:
            raise ValueError('target user not found; ask them to register first')
        target_user_id = int(target_row['id'])
        problem_id = int(ctx['problem']['id'])
        existing = config.db.fetch_one('SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        existing_role = str(existing['role']).strip().lower() if existing is not None else ''
        if existing_role == 'owner' and safe_role != 'owner' and (problem_owner_count(problem_id) <= 1):
            raise ValueError('cannot demote the last owner')
        config.db.execute('\n            INSERT INTO repo_acl(problem_id,user_id,role,created_at)\n            VALUES(?,?,?,?)\n            ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role\n            ', [problem_id, target_user_id, safe_role, now_iso()])
        audit(int(ctx['user']['id']), problem_id, 'access.grant', {'target_user': safe_target, 'role': safe_role})
        msg = f'access updated: {safe_target} -> {safe_role}'
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/access', status_code=303, message=msg)

def workspace_access_revoke(problem: str, user: str, target_user: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_manage_access(ctx)
    msg = 'access removed'
    redirect_to_problems = False
    try:
        safe_target = normalize_username_required(target_user)
        target_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [safe_target])
        if target_row is None:
            raise ValueError('target user not found')
        target_user_id = int(target_row['id'])
        problem_id = int(ctx['problem']['id'])
        existing = config.db.fetch_one('SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        if existing is None:
            raise ValueError('access entry not found')
        existing_role = str(existing['role'] or '').strip().lower()
        if existing_role == 'owner' and problem_owner_count(problem_id) <= 1:
            raise ValueError('cannot remove the last owner')
        config.db.execute('DELETE FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        redirect_to_problems = target_user_id == int(ctx['user']['id'])
        audit(int(ctx['user']['id']), problem_id, 'access.revoke', {'target_user': safe_target})
        msg = f'access removed: {safe_target}'
    except ValueError as exc:
        msg = str(exc)
    if redirect_to_problems:
        return redirect_response('/problems', status_code=303, message=msg)
    return redirect_response(f'/problems/{problem}/{user}/access', status_code=303, message=msg)


