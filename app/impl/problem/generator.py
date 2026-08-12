from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import json_error_response, json_redirect_response, redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.dependency import runtime
from app.impl.problem.shared import _normalize_component_create_path, rename_component_source
from app.impl.workspace.context_operation import (
    generator_sources_from_build_cfg,
    read_build_config,
    template_for_kind,
    workspace_rel_file_exists,
    write_build_config,
)
from app.impl.workspace.context_component_status import generator_status_context
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.platform.workspace_path import (
    normalize_component_source_path,
    normalize_optional_component_source_path_safe,
    safe_workspace_path,
)



def _generator_template_for_target(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == '.py':
        return '#!/usr/bin/env python3\n'
    if suffix == '.java':
        return ''
    return template_for_kind('generator')


def generators_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    generator_status = generator_status_context(workspace)
    source_rows: list[dict[str, object]] = []
    if isinstance(generator_status.get('configured_sources'), list):
        for row in generator_status['configured_sources']:
            if not isinstance(row, dict):
                continue
            path = normalize_optional_component_source_path_safe(row.get('path'), 'generators', 'generator source')
            if not path:
                continue
            if not bool(row.get('exists')):
                continue
            source_rows.append({
                'path': path,
                'configured': bool(row.get('configured')),
                'reference_count': int(row.get('reference_count') or 0),
            })
    requested_source = normalize_optional_component_source_path_safe(request.query_params.get('path'), 'generators', 'generator source')
    requested_new = request.query_params.get('new')
    new_source = ''
    if requested_new is not None:
        try:
            new_source = _normalize_component_create_path(
                requested_new or 'generator.cpp',
                'generators',
                'generator.cpp',
            )
        except ValueError:
            new_source = 'generators/generator.cpp'
    repo_source = generator_status['repo_source'] if isinstance(generator_status.get('repo_source'), str) and generator_status['repo_source'] else 'generators/generator.cpp'
    selected_source = new_source or requested_source or repo_source or 'generators/generator.cpp'
    selected_exists = workspace_rel_file_exists(workspace, selected_source)
    if selected_source and selected_exists and all((row.get('path') != selected_source for row in source_rows)):
        source_rows.insert(0, {'path': selected_source, 'configured': False})
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = safe_workspace_path(workspace, selected_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = runtime().git_service.read_file_limited(workspace, selected_source, runtime().config_values.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    starter_content = _generator_template_for_target(selected_source) if not selected_exists else ''
    show_editor = bool(selected_exists or new_source)
    return template_response(request, 'generators.html', {'ctx': ctx, 'generator_status': generator_status, 'repo_source': selected_source, 'selected_source': selected_source, 'selected_exists': selected_exists, 'new_source': bool(new_source), 'show_editor': show_editor, 'source_rows': source_rows, 'source_rows_truncated': bool(generator_status.get('source_rows_truncated')), 'repo_content': repo_content, 'starter_content': starter_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': runtime().config_values.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def generator_rename_source(
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
        folder='generators',
        default_filename='generator.cpp',
        component_label='generator',
        redirect_url_for_path=lambda path: f'/problems/{problem}/generators?path={quote_plus(path)}',
        config_key='generator_sources',
    )

def generator_save_source(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: str = Form('generators/generator.cpp'),
    content: str = Form(''),
    response_mode: str = Form(''),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'generators/generator.cpp'
    msg = 'generator source saved'
    save_ok = False
    json_requested = str(response_mode or '').strip().lower() == 'json'
    try:
        target = normalize_component_source_path(path, 'generators', 'generator.cpp')
        safe_content = enforce_textarea_max_bytes(
            content,
            label='generator source',
            max_bytes=int(runtime().config_values.TEXTAREA_MAX_BYTES),
        )
        with runtime().workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            generator_sources = generator_sources_from_build_cfg(build_cfg)
            if target not in generator_sources:
                generator_sources.append(target)
            build_cfg['generator_sources'] = generator_sources
            write_build_config(cfg_path, build_cfg)
            runtime().git_service.write_file(workspace, target, safe_content)
            compile_check_error = judgehost_compile_check_error(
                application_runtime=runtime(),
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=safe_content,
                verification_source='problem.generator.save_source',
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
    redirect_url = f'/problems/{problem}/generators?path={quote_plus(target)}'
    if json_requested:
        if save_ok:
            return json_redirect_response(redirect_url, msg)
        return json_error_response(msg)
    return redirect_response(redirect_url, status_code=303, message=msg)
