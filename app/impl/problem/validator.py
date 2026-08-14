from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import json_error_response, json_redirect_response, redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.problem.shared import rename_component_source, single_source_editor_context
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import read_build_config, template_for_kind, write_build_config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_component_status import validator_status_context
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path



def validator_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    validator_status = validator_status_context(
        workspace,
        ctx['authoring_source']['build'],
    )
    repo_source = validator_status['repo_source'] if isinstance(validator_status.get('repo_source'), str) and validator_status['repo_source'] else 'validators/validator.cpp'
    repo_exists = bool(validator_status.get('repo_source_exists'))
    editor = single_source_editor_context(
        request=request,
        workspace=workspace,
        configured_source=repo_source,
        configured_source_exists=repo_exists,
        folder='validators',
        default_filename='validator.cpp',
        starter_content=template_for_kind('validator'),
    )
    return template_response(request, 'validator.html', {'ctx': ctx, 'validator_status': validator_status, 'editor': editor, 'content_char_limit': runtime().config_values.integer("WORKSPACE_FILE_VIEW_CHAR_LIMIT")})

def validator_rename_source(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    old_path: str = Form(...),
    new_path: str = Form(...),
):
    return rename_component_source(
        problem=problem,
        user=user,
        old_path=old_path,
        new_path=new_path,
        folder='validators',
        default_filename='validator.cpp',
        component_label='validator',
        redirect_url_for_path=lambda _path: f'/problems/{problem}/validator',
        config_key='validator_source',
    )

def validator_save_source(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: str = Form('validators/validator.cpp'),
    content: str = Form(''),
    response_mode: str = Form(''),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'validators/validator.cpp'
    msg = 'validator source saved'
    save_ok = False
    json_requested = str(response_mode or '').strip().lower() == 'json'
    try:
        target = normalize_component_source_path(path, 'validators', 'validator.cpp')
        safe_content = enforce_textarea_max_bytes(
            content,
            label='validator source',
            max_bytes=runtime().config_values.integer("TEXTAREA_MAX_BYTES"),
        )
        with runtime().workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg['validator_source'] = target
            write_build_config(cfg_path, build_cfg)
            runtime().git_service.write_file(workspace, target, safe_content)
            compile_check_error = judgehost_compile_check_error(
                application_runtime=runtime(),
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=safe_content,
                verification_source='problem.validator.save_source',
            )
            if compile_check_error:
                if target_existed_before:
                    target_abs.write_bytes(target_previous_bytes)
                else:
                    runtime().git_service.delete_path(workspace, target)
                if cfg_existed_before:
                    cfg_path.write_text(cfg_previous_text, encoding='utf-8')
                else:
                    cfg_path.unlink(missing_ok=True)
                raise ValueError(f'compile check failed: {compile_check_error}')
        save_ok = True
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    redirect_url = f'/problems/{problem}/validator'
    if json_requested:
        if save_ok:
            return json_redirect_response(redirect_url, msg)
        return json_error_response(msg)
    return redirect_response(redirect_url, status_code=303, message=msg)
