from __future__ import annotations

from pathlib import Path

from fastapi import Form

from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx


def git_commit(problem: str, user: str, message: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    commit_created = False
    commit_head = ''
    try:
        with config.workspace_service.workspace_lock(workspace):
            try:
                commit_head = config.git_service.commit(workspace, message, user, f'{user}@polygonlike.local')
                commit_created = True
            except Exception as commit_exc:
                commit_err = str(commit_exc)
                commit_err_lower = commit_err.lower()
                if 'nothing to commit' not in commit_err_lower and 'no changes added to commit' not in commit_err_lower:
                    raise
            try:
                config.git_service.push(workspace, 'main')
            except Exception as push_exc:
                if commit_created:
                    try:
                        config.git_service.rollback_last_commit(workspace, expected_head=commit_head)
                    except Exception as rollback_exc:
                        raise RuntimeError(f'{push_exc}; rollback failed: {rollback_exc}') from rollback_exc
                raise push_exc
        if commit_created:
            audit(ctx['user']['id'], ctx['problem']['id'], 'git.commit', {'message': message, 'head': commit_head})
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.push', {'branch': 'main', 'via': 'commit'})
        msg = 'commit and publish ok' if commit_created else 'publish ok'
    except Exception as exc:
        err = str(exc)
        err_lower = err.lower()
        if 'non-fast-forward' in err_lower or 'fetch first' in err_lower or 'rejected' in err_lower:
            msg = 'publish failed: upstream advanced; rebase required, commit rolled back'
        else:
            msg = err
    return redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_push(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.push(workspace, 'main')
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.push', {'branch': 'main'})
        msg = 'push ok'
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_pull(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.pull(workspace, 'main')
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.pull', {'branch': 'main'})
        msg = 'pull ok'
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_restore_revision(problem: str, user: str, revision: str=Form(...), page: str=Form('history')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target_page = 'workspace' if page.strip().lower() == 'workspace' else 'history'
    try:
        with config.workspace_service.workspace_lock(workspace):
            resolved = config.git_service.restore_revision_to_working_copy(workspace, revision)
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.restore_revision', {'revision': revision, 'resolved_commit': resolved})
        msg = f'restored files from {resolved[:12]} on top of latest main; commit when ready'
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=msg)

def git_rebase_continue(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rebase_continue(workspace)
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.rebase_continue', {})
        msg = 'rebase continue ok'
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_rebase_abort(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rebase_abort(workspace)
        audit(ctx['user']['id'], ctx['problem']['id'], 'git.rebase_abort', {})
        msg = 'rebase aborted'
    except Exception as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)



