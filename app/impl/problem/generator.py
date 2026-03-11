from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request

from app.impl.auth.public import redirect_response, template_response
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.config import config
from app.impl.problem.shared import _normalize_component_create_path
from app.impl.workspace.public import (
    audit,
    generator_sources_from_build_cfg,
    generator_status_context,
    normalize_optional_component_source_path_safe,
    read_build_config,
    require_write_access,
    template_for_kind,
    workspace_rel_file_exists,
    write_build_config,
    page_ctx,
)
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path

_C = config.constants


def generators_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    generator_status = generator_status_context(workspace)
    source_rows_raw = generator_status.get('configured_sources')
    source_rows: list[dict[str, object]] = []
    if isinstance(source_rows_raw, list):
        for row in source_rows_raw:
            if not isinstance(row, dict):
                continue
            path = normalize_optional_component_source_path_safe(row.get('path'), 'generators', 'generator source')
            if not path:
                continue
            source_rows.append({'path': path, 'exists': bool(row.get('exists')), 'configured': bool(row.get('configured'))})
    requested_source = normalize_optional_component_source_path_safe(request.query_params.get('path'), 'generators', 'generator source')
    repo_source = str(generator_status.get('repo_source') or 'generators/generator.cpp')
    selected_source = requested_source or repo_source or 'generators/generator.cpp'
    selected_exists = workspace_rel_file_exists(workspace, selected_source)
    if selected_source and all((str(row.get('path') or '') != selected_source for row in source_rows)):
        source_rows.insert(0, {'path': selected_source, 'exists': selected_exists, 'configured': False})
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = safe_workspace_path(workspace, selected_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, selected_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = template_for_kind('generator')
    create_template_path_default = Path(selected_source).name if selected_source else 'generator.cpp'
    return template_response(request, 'generators.html', {'ctx': ctx, 'generator_status': generator_status, 'repo_source': selected_source, 'selected_source': selected_source, 'selected_exists': selected_exists, 'source_rows': source_rows, 'source_rows_truncated': bool(generator_status.get('source_rows_truncated')), 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'create_template_path_default': create_template_path_default})

def generator_create_template(problem: str, user: str, path: str=Form('generator.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator template created'
    target = 'generators/generator.cpp'
    try:
        target = _normalize_component_create_path(path, 'generators', 'generator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('generator source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'generator source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, template_for_kind('generator'))
            build_cfg, cfg_path = read_build_config(workspace)
            generator_sources = generator_sources_from_build_cfg(build_cfg)
            if target not in generator_sources:
                generator_sources.append(target)
            build_cfg['generator_sources'] = generator_sources
            write_build_config(cfg_path, build_cfg)
        audit(ctx['user']['id'], ctx['problem']['id'], 'generators.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/generators?path={quote_plus(target)}', status_code=303, message=msg)

def generator_save_source(problem: str, user: str, path: str=Form('generators/generator.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'generators/generator.cpp'
    msg = 'generator source saved'
    try:
        target = normalize_component_source_path(path, 'generators', 'generator.cpp')
        with config.workspace_service.workspace_lock(workspace):
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
            config.git_service.write_file(workspace, target, content)
            compile_check_error = judgehost_compile_check_error(
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=content,
                invocation_source='problem.generator.save_source',
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
        audit(ctx['user']['id'], ctx['problem']['id'], 'generators.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/generators?path={quote_plus(target)}', status_code=303, message=msg)





