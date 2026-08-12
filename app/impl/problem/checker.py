from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import json_error_response, json_redirect_response, redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.config import config
from app.impl.problem.shared import rename_component_source, single_source_editor_context
from app.impl.workspace.context_operation import read_build_config, resolve_standard_checker_path, standard_checker_catalog, template_for_kind, write_build_config
from app.impl.workspace.context_component_status import checker_status_context
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path
from app.service.verification.standard_checker import copy_standard_checker

_C = config.config_values


def checker_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    workspace = Path(ctx['workspace']['path'])
    checker_status = checker_status_context(workspace)
    standard_checker_options = standard_checker_catalog()
    selected_standard = standard_checker if isinstance(standard_checker := checker_status.get('standard_checker'), str) else ''
    repo_source = repo_source if isinstance(repo_source := checker_status.get('repo_source'), str) and repo_source else 'checkers/checker.cpp'
    editor = single_source_editor_context(
        request=request,
        workspace=workspace,
        configured_source=repo_source,
        configured_source_exists=bool(checker_status.get('repo_source_exists')),
        folder='checkers',
        default_filename='checker.cpp',
        starter_content=template_for_kind('checker'),
    )
    show_custom_editor = editor['state'] == 'create' or bool(
        editor['state'] == 'existing' and (
            not checker_status.get('standard_checker')
            or request.query_params.get('edit') == 'source'
        )
    )
    return template_response(request, 'checker.html', {'ctx': ctx, 'checker_status': checker_status, 'standard_checker_options': standard_checker_options, 'selected_standard_checker': selected_standard, 'show_custom_editor': show_custom_editor, 'editor': editor, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def checker_view_standard(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)], checker_name: str=''):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    workspace = Path(ctx['workspace']['path'])
    checker_status = checker_status_context(workspace)
    selected = checker_name.strip()
    if not selected:
        selected = standard_checker if isinstance(standard_checker := checker_status.get('standard_checker'), str) else ''
    if not selected:
        catalog = standard_checker_catalog()
        if catalog:
            selected = catalog[0]['value']
    if not selected:
        return redirect_response(f'/problems/{problem}/checker', status_code=303, message='no standard checker available')
    try:
        normalized_name, source_path = resolve_standard_checker_path(selected)
        canonical = f'std::{normalized_name}'
        source_text = source_path.read_text(encoding='utf-8', errors='replace')
    except ValueError as exc:
        return redirect_response(f'/problems/{problem}/checker', status_code=303, message=str(exc))
    except OSError as exc:
        return redirect_response(f'/problems/{problem}/checker', status_code=303, message=str(exc))
    return template_response(request, 'checker_standard_view.html', {'ctx': ctx, 'checker_name': canonical, 'checker_source': source_text})

def checker_set_standard(problem: str, user: Annotated[str, Depends(require_session_user)], checker_name: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'checker updated'
    try:
        normalized_name, _ = resolve_standard_checker_path(checker_name)
        canonical = f'std::{normalized_name}'
        with config.workspace_service.workspace_lock(workspace):
            checker_rel = copy_standard_checker(normalized_name, workspace)
            build_cfg, cfg_path = read_build_config(workspace)
            build_cfg['checker_source'] = checker_rel
            write_build_config(cfg_path, build_cfg)
        msg = f'checker set to {canonical}'
    except ValueError as exc:
        msg = str(exc)
    except OSError as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/checker', status_code=303, message=msg)

def checker_rename_source(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    old_path: str = Form(...),
    new_path: str = Form(...),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    return rename_component_source(
        problem=problem,
        user=user,
        old_path=old_path,
        new_path=new_path,
        folder='checkers',
        default_filename='checker.cpp',
        component_label='checker',
        redirect_url_for_path=lambda _path: f'/problems/{problem}/checker',
        config_key='checker_source',
        ctx=ctx,
    )

def checker_save_source(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: str = Form('checkers/checker.cpp'),
    content: str = Form(''),
    response_mode: str = Form(''),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'checkers/checker.cpp'
    msg = 'checker source saved'
    save_ok = False
    json_requested = str(response_mode or '').strip().lower() == 'json'
    try:
        target = normalize_component_source_path(path, 'checkers', 'checker.cpp')
        safe_content = enforce_textarea_max_bytes(
            content,
            label='checker source',
            max_bytes=int(_C.TEXTAREA_MAX_BYTES),
        )
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg['checker_source'] = target
            write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, safe_content)
            compile_check_error = judgehost_compile_check_error(
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=safe_content,
                verification_source='problem.checker.save_source',
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
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    redirect_url = f'/problems/{problem}/checker'
    if json_requested:
        if save_ok:
            return json_redirect_response(redirect_url, msg)
        return json_error_response(msg)
    return redirect_response(redirect_url, status_code=303, message=msg)
