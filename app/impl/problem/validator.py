from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import json_error_response, json_redirect_response, redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.problem.shared import rename_component_source, single_source_editor_context
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import read_build_config, template_for_kind, write_build_config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.platform.workspace_path import normalize_component_source_path



def validator_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    validator_status = ctx['shell']['components']['validator']
    repo_source = validator_status['repo_source'] or 'validators/validator.cpp'
    repo_exists = validator_status['repo_source_exists']
    editor = single_source_editor_context(
        request=request,
        workspace=workspace,
        configured_source=repo_source,
        configured_source_exists=repo_exists,
        folder='validators',
        default_filename='validator.cpp',
        starter_content=template_for_kind('validator'),
    )
    return template_response(request, 'validator.html', {'ctx': ctx, 'editor': editor, 'content_char_limit': runtime().config_values.integer("WORKSPACE_FILE_VIEW_CHAR_LIMIT")})

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
            build_cfg, cfg_path = read_build_config(workspace)
            build_cfg['validator_source'] = target
            write_build_config(cfg_path, build_cfg)
            runtime().git_service.write_file(workspace, target, safe_content)
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
