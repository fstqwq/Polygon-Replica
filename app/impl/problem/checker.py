from __future__ import annotations

from pathlib import Path

from fastapi import Form, HTTPException, Request

from app.impl.auth.shared import redirect_response, template_response
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit, read_build_config, resolve_standard_checker_path, standard_checker_catalog, template_for_kind, write_build_config
from app.impl.workspace.context_component_status import checker_status_context
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.service.platform.workspace_path import normalize_component_source_path, safe_workspace_path
from app.service.verification.standard_checker import copy_standard_checker

_C = config.constants


def checker_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    workspace = Path(ctx['workspace']['path'])
    checker_status = checker_status_context(workspace)
    standard_checker_options = standard_checker_catalog()
    selected_standard = standard_checker if isinstance(standard_checker := checker_status.get('standard_checker'), str) else ''
    if not selected_standard and standard_checker_options:
        selected_standard = standard_checker_options[0]['value']
    repo_source = repo_source if isinstance(repo_source := checker_status.get('repo_source'), str) and repo_source else 'checkers/checker.cpp'
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
        repo_content = template_for_kind('checker')
    return template_response(request, 'checker.html', {'ctx': ctx, 'checker_status': checker_status, 'standard_checker_options': standard_checker_options, 'selected_standard_checker': selected_standard, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def checker_view_standard(request: Request, problem: str, user: str, checker_name: str=''):
    ctx = page_ctx(problem, user)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
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
        return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message='no standard checker available')
    try:
        normalized_name, source_path = resolve_standard_checker_path(selected)
        canonical = f'std::{normalized_name}'
        source_text = source_path.read_text(encoding='utf-8', errors='replace')
        description = str(_C.STANDARD_CHECKER_DESCRIPTIONS.get(normalized_name, 'general-purpose standard checker from testlib'))
    except ValueError as exc:
        return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=str(exc))
    except OSError as exc:
        return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=str(exc))
    return template_response(request, 'checker_standard_view.html', {'ctx': ctx, 'checker_name': canonical, 'checker_description': description, 'checker_source': source_text, 'checker_lines': len(source_text.splitlines())})

def checker_set_standard(problem: str, user: str, checker_name: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'checker updated'
    try:
        normalized_name, _ = resolve_standard_checker_path(checker_name)
        canonical = f'std::{normalized_name}'
        with config.workspace_service.workspace_lock(workspace):
            checker_rel = copy_standard_checker(normalized_name, workspace / 'checkers')
            build_cfg, cfg_path = read_build_config(workspace)
            build_cfg['checker_source'] = checker_rel
            write_build_config(cfg_path, build_cfg)
        audit(ctx['user']['id'], ctx['problem']['id'], 'checker.set_standard', {'checker': canonical})
        msg = f'checker set to {canonical}'
    except ValueError as exc:
        msg = str(exc)
    except OSError as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)

def checker_create_template(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'checker template created'
    try:
        with config.workspace_service.workspace_lock(workspace):
            build_cfg, cfg_path = read_build_config(workspace)
            write_build_config(cfg_path, build_cfg)
            checker_path = 'checkers/checker.cpp'
            checker_abs = safe_workspace_path(workspace, checker_path)
            if checker_abs.exists() and checker_abs.is_dir():
                raise ValueError('checker.cpp target is a directory')
            if checker_abs.exists() and checker_abs.is_file() and (checker_abs.stat().st_size > 0):
                msg = 'checker.cpp already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, checker_path, template_for_kind('checker'))
        audit(ctx['user']['id'], ctx['problem']['id'], 'checker.create_template', {'path': 'checkers/checker.cpp'})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)

def checker_save_source(problem: str, user: str, path: str=Form('checkers/checker.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    if ctx.get('problem_mode') == 'interactive':
        return redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message='interactive problem uses an interactor; checker section hidden')
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'checkers/checker.cpp'
    msg = 'checker source saved'
    try:
        target = normalize_component_source_path(path, 'checkers', 'checker.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg['checker_source'] = target
            write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
            compile_check_error = judgehost_compile_check_error(
                problem=problem,
                user=user,
                workspace=workspace,
                source_path=target,
                source_content=content,
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
        audit(ctx['user']['id'], ctx['problem']['id'], 'checker.save_source', {'path': target, 'bytes': len(content.encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)





