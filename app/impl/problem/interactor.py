from __future__ import annotations

from pathlib import Path

from fastapi import Form, HTTPException, Request

from app.impl.auth.shared import json_error_response, json_redirect_response, redirect_response, template_response
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit, read_build_config, template_for_kind, write_build_config
from app.impl.workspace.context_component_status import interactor_status_context
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path

_C = config.constants


def interactor_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    interactor_status = interactor_status_context(workspace)
    repo_source = repo_source if isinstance(repo_source := interactor_status.get('repo_source'), str) and repo_source else 'interactors/interactor.cpp'
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = safe_workspace_path(workspace, repo_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, repo_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = template_for_kind('interactor')
    return template_response(request, 'interactor.html', {'ctx': ctx, 'interactor_status': interactor_status, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def interactor_create_template(problem: str, user: str, path: str=Form('interactors/interactor.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'interactor template created'
    target = 'interactors/interactor.cpp'
    try:
        target = normalize_component_source_path(path, 'interactors', 'interactor.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('interactor source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'interactor source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, template_for_kind('interactor'))
        audit(ctx['user']['id'], ctx['problem']['id'], 'interactor.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message=msg)

def interactor_save_source(
    problem: str,
    user: str,
    path: str = Form('interactors/interactor.cpp'),
    content: str = Form(''),
    response_mode: str = Form(''),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'interactors/interactor.cpp'
    msg = 'interactor source saved'
    save_ok = False
    json_requested = str(response_mode or '').strip().lower() == 'json'
    try:
        target = normalize_component_source_path(path, 'interactors', 'interactor.cpp')
        safe_content = enforce_textarea_max_bytes(content, label='interactor source')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg['interactor_source'] = target
            write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, safe_content)
            compile_check_error = judgehost_compile_check_error(
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=safe_content,
                verification_source='problem.interactor.save_source',
            )
            if compile_check_error:
                if target_existed_before:
                    target_abs.write_bytes(target_previous_bytes)
                else:
                    config.git_service.delete_path(workspace, target)
                if cfg_existed_before:
                    cfg_path.write_text(cfg_previous_text, encoding='utf-8')
                else:
                    cfg_path.unlink(missing_ok=True)
                raise ValueError(f'compile check failed: {compile_check_error}')
        save_ok = True
        audit(ctx['user']['id'], ctx['problem']['id'], 'interactor.save_source', {'path': target, 'bytes': len(safe_content.encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    redirect_url = f'/problems/{problem}/{user}/interactor'
    if json_requested:
        if save_ok:
            return json_redirect_response(redirect_url, msg)
        return json_error_response(msg)
    return redirect_response(redirect_url, status_code=303, message=msg)





