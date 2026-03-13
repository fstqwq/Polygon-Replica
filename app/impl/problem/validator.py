from __future__ import annotations

from pathlib import Path

from fastapi import Form, HTTPException, Request

from app.impl.auth.public import redirect_response, template_response
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.config import config
from app.impl.workspace.public import (
    audit,
    read_build_config,
    require_write_access,
    template_for_kind,
    validator_status_context,
    write_build_config,
    page_ctx,
)
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path

_C = config.constants


def validator_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    validator_status = validator_status_context(workspace)
    repo_source = str(validator_status.get('repo_source') or 'validators/validator.cpp')
    repo_exists = bool(validator_status.get('repo_source_exists'))
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = safe_workspace_path(workspace, repo_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, repo_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if (not repo_exists) and (not repo_content):
        repo_content = template_for_kind('validator')
    return template_response(request, 'validator.html', {'ctx': ctx, 'validator_status': validator_status, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def validator_create_template(problem: str, user: str, path: str=Form('validators/validator.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'validator template created'
    target = 'validators/validator.cpp'
    try:
        target = normalize_component_source_path(path, 'validators', 'validator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('validator source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'validator source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, template_for_kind('validator'))
        audit(ctx['user']['id'], ctx['problem']['id'], 'validator.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/validator', status_code=303, message=msg)

def validator_save_source(problem: str, user: str, path: str=Form('validators/validator.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'validators/validator.cpp'
    msg = 'validator source saved'
    try:
        target = normalize_component_source_path(path, 'validators', 'validator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg['validator_source'] = target
            write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
            compile_check_error = judgehost_compile_check_error(
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=content,
                verification_source='problem.validator.save_source',
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
        audit(ctx['user']['id'], ctx['problem']['id'], 'validator.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/validator', status_code=303, message=msg)




