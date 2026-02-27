from __future__ import annotations
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from app.impl.auth import (
    _create_session_for_user,
    _dummy_password_salt_hex,
    _issue_password_form_csrf_token,
    _login_redirect,
    _lookup_user_auth,
    _normalize_password_iters,
    _normalize_password_salt_hex,
    _normalize_password_verifier_hex,
    _normalize_username_required,
    _password_proof_from_verifier,
    _redirect_response,
    _session_user,
    _set_flash_cookie,
    _set_user_password,
    _set_user_password_verifier,
    _template_response,
    _verify_password_form_csrf_token,
    _verify_user_password,
)
from app.impl.build_preview import preview_page as combined_general_statement_page
from app.impl.config import config
from app.db import now_iso
from app.main_utils import (
    _normalize_component_source_path,
    _normalize_optional_component_source_path_safe,
    _normalize_workspace_rel_path,
    _safe_workspace_path,
)
from app.services.solution_metadata import (
    desc_rel_path_for_source,
    normalize_expected_behavior,
    parse_solution_desc,
    render_solution_desc,
)
from app.services.statement_template import render_statement_main

from app.impl.workspace import (
    _audit,
    _build_line_focus_context,
    _build_repo_browser_entries,
    _checker_status_context,
    _coerce_int,
    _default_files_selected_path,
    _ensure_solution_metadata_for_source,
    _files_back_target,
    _files_browse_query_tail,
    _files_source_query_tail,
    _form_text,
    _generator_sources_from_build_cfg,
    _generator_status_context,
    _interactor_status_context,
    _is_system_admin_user_id,
    _kind_for_path,
    _list_solution_entries,
    _normalize_files_source,
    _normalize_page_target,
    _normalize_problem_mode,
    _normalize_problem_name_required,
    _normalize_repo_role,
    _normalize_solution_source_path_required,
    _normalize_source_id,
    _parse_line_param,
    _problem_owner_count,
    _read_build_config,
    _read_problem_config,
    _read_text_safe_limited,
    _render_workspace_page,
    _require_manage_access,
    _require_system_admin,
    _require_write_access,
    _resolve_build_accepted_solution_source,
    _resolve_standard_checker_path,
    _solution_behavior_options,
    _solution_compile_check_error,
    _solution_metadata_entry,
    _standard_checker_catalog,
    _template_for_kind,
    _user_participating_problems,
    _validator_status_context,
    _workspace_access_context,
    _workspace_rel_file_exists,
    _workspace_source_compile_check_error,
    _write_build_config,
    page_ctx,
)

_C = config.constants

MAIN_CORRECT_EXPECTED_VALUE = 'main_correct'
MAIN_CORRECT_EXPECTED_LABEL = 'main correct solution (AC)'


def _normalize_component_create_path(raw: str | None, folder: str, default_filename: str) -> str:
    normalized = _normalize_workspace_rel_path(raw)
    expected_prefix = f'{folder}/'
    if normalized and (not normalized.startswith(expected_prefix)):
        normalized = f'{folder}/{normalized}'
    return _normalize_component_source_path(normalized, folder, default_filename)


def _normalize_component_user_path_required(raw: str | None, folder: str, label: str, default_filename: str) -> str:
    normalized = _normalize_workspace_rel_path(raw)
    if not normalized:
        raise ValueError(f'{label} is required')
    expected_prefix = f'{folder}/'
    if not normalized.startswith(expected_prefix):
        normalized = f'{folder}/{normalized}'
    return _normalize_component_source_path(normalized, folder, default_filename)

def general_page(request: Request, problem: str, user: str):
    return combined_general_statement_page(request, problem, user)

def general_save(problem: str, user: str, time_limit_ms: str=Form('2000'), memory_limit_mb: str=Form('1024'), mode: str=Form('pass-fail'), problem_name: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        safe_time_limit = _coerce_int(time_limit_ms, int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS)
        safe_memory = _coerce_int(memory_limit_mb, int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB)
        safe_mode = _normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        requested_problem_name = str(problem_name or '').strip()
        current_problem_name = str(ctx['problem'].get('name') or '').strip()
        safe_problem_name = _normalize_problem_name_required(requested_problem_name or current_problem_name)
        with config.workspace_service.workspace_lock(workspace):
            payload, _, cfg_path = _read_problem_config(workspace)
            payload.pop('interactive', None)
            payload.update({'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode})
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            config.workspace_service.set_problem_name(problem, safe_problem_name)
            render_statement_main(workspace / 'statement', problem_title=safe_problem_name)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'general.save', {'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode, 'problem_name': safe_problem_name})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/general', status_code=303, message=msg)

def generators_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    generator_status = _generator_status_context(workspace)
    source_rows_raw = generator_status.get('configured_sources')
    source_rows: list[dict[str, object]] = []
    if isinstance(source_rows_raw, list):
        for row in source_rows_raw:
            if not isinstance(row, dict):
                continue
            path = _normalize_optional_component_source_path_safe(row.get('path'), 'generators', 'generator source')
            if not path:
                continue
            source_rows.append({'path': path, 'exists': bool(row.get('exists')), 'configured': bool(row.get('configured'))})
    requested_source = _normalize_optional_component_source_path_safe(request.query_params.get('path'), 'generators', 'generator source')
    repo_source = str(generator_status.get('repo_source') or 'generators/generator.cpp')
    selected_source = requested_source or repo_source or 'generators/generator.cpp'
    selected_exists = _workspace_rel_file_exists(workspace, selected_source)
    if selected_source and all((str(row.get('path') or '') != selected_source for row in source_rows)):
        source_rows.insert(0, {'path': selected_source, 'exists': selected_exists, 'configured': False})
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = _safe_workspace_path(workspace, selected_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, selected_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = _template_for_kind('generator')
    create_template_path_default = Path(selected_source).name if selected_source else 'generator.cpp'
    return _template_response(request, 'generators.html', {'ctx': ctx, 'generator_status': generator_status, 'repo_source': selected_source, 'selected_source': selected_source, 'selected_exists': selected_exists, 'source_rows': source_rows, 'source_rows_truncated': bool(generator_status.get('source_rows_truncated')), 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'create_template_path_default': create_template_path_default})

def generator_create_template(problem: str, user: str, path: str=Form('generator.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator template created'
    target = 'generators/generator.cpp'
    try:
        target = _normalize_component_create_path(path, 'generators', 'generator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('generator source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'generator source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, _template_for_kind('generator'))
            build_cfg, cfg_path = _read_build_config(workspace)
            generator_sources = _generator_sources_from_build_cfg(build_cfg)
            if target not in generator_sources:
                generator_sources.append(target)
            build_cfg['generator_sources'] = generator_sources
            _write_build_config(cfg_path, build_cfg)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'generators.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/generators?path={quote_plus(target)}', status_code=303, message=msg)

def generator_save_source(problem: str, user: str, path: str=Form('generators/generator.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'generators/generator.cpp'
    msg = 'generator source saved'
    try:
        target = _normalize_component_source_path(path, 'generators', 'generator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = _read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            generator_sources = _generator_sources_from_build_cfg(build_cfg)
            if target not in generator_sources:
                generator_sources.append(target)
            build_cfg['generator_sources'] = generator_sources
            _write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
            compile_check_error = _workspace_source_compile_check_error(workspace, target)
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
        _audit(ctx['user']['id'], ctx['problem']['id'], 'generators.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/generators?path={quote_plus(target)}', status_code=303, message=msg)

def checker_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    checker_status = _checker_status_context(workspace)
    standard_checker_options = _standard_checker_catalog()
    selected_standard = str(checker_status.get('standard_checker') or '')
    if not selected_standard and standard_checker_options:
        selected_standard = str(standard_checker_options[0]['value'])
    repo_source = str(checker_status.get('repo_source') or 'checkers/checker.cpp')
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = _safe_workspace_path(workspace, repo_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, repo_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = _template_for_kind('checker')
    return _template_response(request, 'checker.html', {'ctx': ctx, 'checker_status': checker_status, 'standard_checker_options': standard_checker_options, 'selected_standard_checker': selected_standard, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def checker_view_standard(request: Request, problem: str, user: str, checker_name: str=''):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    checker_status = _checker_status_context(workspace)
    selected = str(checker_name or '').strip()
    if not selected:
        selected = str(checker_status.get('standard_checker') or '')
    if not selected:
        catalog = _standard_checker_catalog()
        if catalog:
            selected = str(catalog[0].get('value') or '')
    if not selected:
        return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message='no standard checker available')
    try:
        normalized_name, source_path = _resolve_standard_checker_path(selected)
        canonical = f'std::{normalized_name}'
        source_text = source_path.read_text(encoding='utf-8', errors='replace')
        description = str(_C.STANDARD_CHECKER_DESCRIPTIONS.get(normalized_name, 'general-purpose standard checker from testlib'))
    except ValueError as exc:
        return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=str(exc))
    except OSError as exc:
        return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=str(exc))
    return _template_response(request, 'checker_standard_view.html', {'ctx': ctx, 'checker_name': canonical, 'checker_description': description, 'checker_source': source_text, 'checker_lines': len(source_text.splitlines())})

def checker_set_standard(problem: str, user: str, checker_name: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'checker updated'
    try:
        normalized_name, _ = _resolve_standard_checker_path(checker_name)
        canonical = f'std::{normalized_name}'
        removed_repo_checker = False
        with config.workspace_service.workspace_lock(workspace):
            build_cfg, cfg_path = _read_build_config(workspace)
            build_cfg['checker_standard'] = canonical
            build_cfg.pop('checker_source', None)
            _write_build_config(cfg_path, build_cfg)
            checker_cpp = _safe_workspace_path(workspace, 'checkers/checker.cpp')
            if checker_cpp.exists() and checker_cpp.is_file():
                checker_cpp.unlink()
                removed_repo_checker = True
        _audit(ctx['user']['id'], ctx['problem']['id'], 'checker.set_standard', {'checker': canonical, 'removed_checker_cpp': removed_repo_checker})
        msg = f'checker set to {canonical}'
    except ValueError as exc:
        msg = str(exc)
    except OSError as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)

def checker_create_template(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'checker template created'
    try:
        with config.workspace_service.workspace_lock(workspace):
            build_cfg, cfg_path = _read_build_config(workspace)
            build_cfg.pop('checker_standard', None)
            _write_build_config(cfg_path, build_cfg)
            checker_path = 'checkers/checker.cpp'
            checker_abs = _safe_workspace_path(workspace, checker_path)
            if checker_abs.exists() and checker_abs.is_dir():
                raise ValueError('checker.cpp target is a directory')
            if checker_abs.exists() and checker_abs.is_file() and (checker_abs.stat().st_size > 0):
                msg = 'checker.cpp already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, checker_path, _template_for_kind('checker'))
        _audit(ctx['user']['id'], ctx['problem']['id'], 'checker.create_template', {'path': 'checkers/checker.cpp'})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)

def checker_save_source(problem: str, user: str, path: str=Form('checkers/checker.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'checkers/checker.cpp'
    msg = 'checker source saved'
    try:
        target = _normalize_component_source_path(path, 'checkers', 'checker.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            target_existed_before = bool(target_abs.exists() and target_abs.is_file() and (not target_abs.is_symlink()))
            target_previous_bytes = target_abs.read_bytes() if target_existed_before else b''
            build_cfg, cfg_path = _read_build_config(workspace)
            cfg_existed_before = bool(cfg_path.exists() and cfg_path.is_file() and (not cfg_path.is_symlink()))
            cfg_previous_text = cfg_path.read_text(encoding='utf-8') if cfg_existed_before else ''
            build_cfg.pop('checker_standard', None)
            build_cfg['checker_source'] = target
            _write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
            compile_check_error = _workspace_source_compile_check_error(workspace, target)
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
        _audit(ctx['user']['id'], ctx['problem']['id'], 'checker.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/checker', status_code=303, message=msg)

def validator_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    validator_status = _validator_status_context(workspace)
    repo_source = str(validator_status.get('repo_source') or 'validators/validator.cpp')
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = _safe_workspace_path(workspace, repo_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, repo_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = _template_for_kind('validator')
    return _template_response(request, 'validator.html', {'ctx': ctx, 'validator_status': validator_status, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def validator_create_template(problem: str, user: str, path: str=Form('validators/validator.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'validator template created'
    target = 'validators/validator.cpp'
    try:
        target = _normalize_component_source_path(path, 'validators', 'validator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('validator source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'validator source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, _template_for_kind('validator'))
        _audit(ctx['user']['id'], ctx['problem']['id'], 'validator.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/validator', status_code=303, message=msg)

def validator_save_source(problem: str, user: str, path: str=Form('validators/validator.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'validators/validator.cpp'
    msg = 'validator source saved'
    try:
        target = _normalize_component_source_path(path, 'validators', 'validator.cpp')
        with config.workspace_service.workspace_lock(workspace):
            build_cfg, cfg_path = _read_build_config(workspace)
            build_cfg['validator_source'] = target
            _write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'validator.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/validator', status_code=303, message=msg)

def interactor_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    interactor_status = _interactor_status_context(workspace)
    repo_source = str(interactor_status.get('repo_source') or 'interactors/interactor.cpp')
    repo_content = ''
    repo_content_truncated = False
    try:
        repo_abs = _safe_workspace_path(workspace, repo_source)
        if repo_abs.exists() and repo_abs.is_file() and (not repo_abs.is_symlink()):
            repo_content, repo_content_truncated = config.git_service.read_file_limited(workspace, repo_source, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        repo_content = ''
        repo_content_truncated = False
    if not repo_content:
        repo_content = _template_for_kind('interactor')
    return _template_response(request, 'interactor.html', {'ctx': ctx, 'interactor_status': interactor_status, 'repo_source': repo_source, 'repo_content': repo_content, 'repo_content_truncated': repo_content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT})

def interactor_create_template(problem: str, user: str, path: str=Form('interactors/interactor.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'interactor template created'
    target = 'interactors/interactor.cpp'
    try:
        target = _normalize_component_source_path(path, 'interactors', 'interactor.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('interactor source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'interactor source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, _template_for_kind('interactor'))
        _audit(ctx['user']['id'], ctx['problem']['id'], 'interactor.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message=msg)

def interactor_save_source(problem: str, user: str, path: str=Form('interactors/interactor.cpp'), content: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target = 'interactors/interactor.cpp'
    msg = 'interactor source saved'
    try:
        target = _normalize_component_source_path(path, 'interactors', 'interactor.cpp')
        with config.workspace_service.workspace_lock(workspace):
            build_cfg, cfg_path = _read_build_config(workspace)
            build_cfg['interactor_source'] = target
            _write_build_config(cfg_path, build_cfg)
            config.git_service.write_file(workspace, target, content)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'interactor.save_source', {'path': target, 'bytes': len(str(content or '').encode('utf-8'))})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/interactor', status_code=303, message=msg)

def solutions_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    entries, entries_truncated = _list_solution_entries(workspace)
    selected = _normalize_workspace_rel_path(request.query_params.get('path'))
    if not selected or not any((str(row.get('source_path')) == selected for row in entries)):
        selected = str(entries[0].get('source_path')) if entries else ''
    selected_entry = next((row for row in entries if str(row.get('source_path')) == selected), None)
    accepted_source = _resolve_build_accepted_solution_source(workspace, entries)
    accepted_source_exists = bool(accepted_source) and _workspace_rel_file_exists(workspace, accepted_source)
    expected_behavior_options = [{'value': MAIN_CORRECT_EXPECTED_VALUE, 'label': MAIN_CORRECT_EXPECTED_LABEL}, *_solution_behavior_options()]
    entries_view: list[dict] = []
    for row in entries:
        row_view = dict(row)
        source_path = str(row_view.get('source_path') or '')
        raw_expected = normalize_expected_behavior(str(row_view.get('expected_behavior') or 'unknown'))
        effective_expected = raw_expected
        # Main correct solution is controlled by build config and must be unique in UI.
        if accepted_source_exists and source_path == accepted_source:
            effective_expected = MAIN_CORRECT_EXPECTED_VALUE
        row_view['expected_behavior_effective'] = effective_expected
        entries_view.append(row_view)
    solution_create_default_path = 'accepted.cpp'
    return _template_response(request, 'solutions.html', {'ctx': ctx, 'entries': entries_view, 'entries_truncated': entries_truncated, 'entries_limit': _C.SOLUTION_LIST_LIMIT, 'selected': selected, 'selected_entry': selected_entry, 'expected_behavior_options': expected_behavior_options, 'accepted_source': accepted_source, 'accepted_source_exists': accepted_source_exists, 'solution_create_default_path': solution_create_default_path})

def solutions_create_template(problem: str, user: str, path: str=Form('accepted.cpp')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'solution file created'
    target = 'solutions/accepted.cpp'
    metadata_created = False
    try:
        target = _normalize_component_create_path(path, 'solutions', 'accepted.cpp')
        with config.workspace_service.workspace_lock(workspace):
            target_abs = _safe_workspace_path(workspace, target)
            if target_abs.exists() and target_abs.is_dir():
                raise ValueError('solution source target is a directory')
            if target_abs.exists() and target_abs.is_file() and (target_abs.stat().st_size > 0):
                msg = 'solution source already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, target, '')
                metadata_created = _ensure_solution_metadata_for_source(workspace, target)
                if metadata_created:
                    msg = 'solution file and metadata created'
        _audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.create_template', {'path': target})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/solutions/editor?path={quote_plus(target)}', status_code=303, message=msg)

def solutions_editor_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    entries, entries_truncated = _list_solution_entries(workspace)
    requested = _normalize_workspace_rel_path(request.query_params.get('path'))
    selected = ''
    if requested:
        try:
            selected = _normalize_solution_source_path_required(requested)
        except ValueError:
            selected = ''
    if not selected:
        selected = str(entries[0].get('source_path')) if entries else 'solutions/accepted.cpp'
    selected_entry = next((row for row in entries if str(row.get('source_path')) == selected), None)
    selected_exists = False
    content = ''
    content_truncated = False
    try:
        selected_abs = _safe_workspace_path(workspace, selected)
        if selected_abs.exists() and selected_abs.is_file() and (not selected_abs.is_symlink()):
            selected_exists = True
            content, content_truncated = config.git_service.read_file_limited(workspace, selected, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        selected_exists = False
        content = ''
        content_truncated = False
    if selected_entry is None:
        selected_entry = _solution_metadata_entry(workspace, selected)
    return _template_response(request, 'solutions_editor.html', {'ctx': ctx, 'entries': entries, 'entries_truncated': entries_truncated, 'entries_limit': _C.SOLUTION_LIST_LIMIT, 'selected': selected, 'selected_entry': selected_entry, 'selected_exists': selected_exists, 'content': content, 'content_truncated': content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'expected_behavior_options': _solution_behavior_options()})

def solutions_save_source(request: Request, problem: str, user: str, source_path: str=Form(...), content: str=Form(''), expected_behavior: str=Form('unknown')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = 'solutions/accepted.cpp'
    selected_for_redirect = selected
    msg = 'solution source saved'
    save_ok = False
    metadata_created = False
    normalized_expected = 'unknown'
    json_requested = str(request.headers.get('x-requested-with') or '').strip().lower() in {'fetch', 'xmlhttprequest'}
    if not json_requested:
        accept = str(request.headers.get('accept') or '').lower()
        json_requested = 'application/json' in accept
    try:
        selected = _normalize_solution_source_path_required(source_path)
        normalized_expected = normalize_expected_behavior(expected_behavior)
        selected_for_redirect = selected
        with config.workspace_service.workspace_lock(workspace):
            selected_abs = _safe_workspace_path(workspace, selected)
            existed_before = selected_abs.exists() and selected_abs.is_file() and (not selected_abs.is_symlink())
            previous_bytes = selected_abs.read_bytes() if existed_before else b''
            desc_path = desc_rel_path_for_source(selected)
            desc_abs = _safe_workspace_path(workspace, desc_path)
            desc_existed_before = desc_abs.exists() and desc_abs.is_file() and (not desc_abs.is_symlink())
            desc_note = ''
            if desc_existed_before:
                desc_text, _ = _read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
                parsed_desc = parse_solution_desc(desc_text)
                desc_note = str(parsed_desc.get('note') or '')
            config.git_service.write_file(workspace, selected, content)
            compile_check_error = _solution_compile_check_error(workspace, selected)
            if compile_check_error:
                if existed_before:
                    selected_abs.write_bytes(previous_bytes)
                else:
                    config.git_service.delete_path(workspace, selected)
                raise ValueError(compile_check_error)
            config.git_service.write_file(workspace, desc_path, render_solution_desc(normalized_expected, desc_note))
            metadata_created = not desc_existed_before
        if metadata_created:
            msg = 'solution source and metadata saved'
        elif normalized_expected != 'unknown':
            msg = f'solution source saved ({normalized_expected})'
        save_ok = True
        _audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.save_source', {'path': selected, 'bytes': len(str(content or '').encode('utf-8')), 'metadata_created': metadata_created, 'expected_behavior': normalized_expected})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    editor_url = f'/problems/{problem}/{user}/solutions/editor?path={quote_plus(selected_for_redirect)}'
    if json_requested:
        if save_ok:
            response = JSONResponse({'ok': True, 'redirect': editor_url, 'message': msg})
            _set_flash_cookie(response, [msg])
            return response
        return JSONResponse({'ok': False, 'error': msg}, status_code=400)
    return _redirect_response(editor_url, status_code=303, message=msg)

def solutions_set_tag(problem: str, user: str, source_path: str=Form(...), expected_behavior: str=Form('unknown')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = 'solutions/accepted.cpp'
    msg = 'solution tag updated'
    try:
        selected = _normalize_solution_source_path_required(source_path)
        source_abs = _safe_workspace_path(workspace, selected)
        if source_abs.is_symlink() or not source_abs.exists() or (not source_abs.is_file()):
            raise ValueError('solution source does not exist')
        raw_expected = str(expected_behavior or '').strip().lower()
        is_main_correct = raw_expected in {MAIN_CORRECT_EXPECTED_VALUE, 'main-correct', 'maincorrect'}
        normalized_expected = 'accepted' if is_main_correct else normalize_expected_behavior(expected_behavior)
        desc_path = desc_rel_path_for_source(selected)
        note = ''
        with config.workspace_service.workspace_lock(workspace):
            if _workspace_rel_file_exists(workspace, desc_path):
                desc_abs = _safe_workspace_path(workspace, desc_path)
                desc_text, _ = _read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
                parsed = parse_solution_desc(desc_text)
                note = str(parsed.get('note') or '')
            build_cfg, cfg_path = _read_build_config(workspace)
            configured = _normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
            build_cfg_changed = False
            if is_main_correct:
                if configured != selected:
                    build_cfg['accepted_solution_source'] = selected
                    build_cfg_changed = True
            elif configured == selected:
                build_cfg.pop('accepted_solution_source', None)
                build_cfg_changed = True
            if build_cfg_changed:
                _write_build_config(cfg_path, build_cfg)
            if normalized_expected == 'unknown' and (not note):
                config.git_service.delete_path(workspace, desc_path)
                msg = 'solution tag cleared'
            else:
                config.git_service.write_file(workspace, desc_path, render_solution_desc(normalized_expected, note))
                if is_main_correct:
                    msg = 'solution tag set to main correct solution (AC)'
                else:
                    msg = f'solution tag set to {normalized_expected}'
        _audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.set_tag', {'source': selected, 'expected_behavior': normalized_expected, 'main_correct': bool(is_main_correct)})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/solutions?path={quote_plus(selected)}', status_code=303, message=msg)

def solutions_rename(problem: str, user: str, old_path: str=Form(...), new_path: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = _normalize_workspace_rel_path(old_path)
    msg = 'solution renamed'
    try:
        old_source = _normalize_solution_source_path_required(old_path)
        new_source = _normalize_component_user_path_required(new_path, 'solutions', 'new solution source', 'accepted.cpp')
        selected = old_source
        if old_source == new_source:
            msg = 'solution rename skipped'
        else:
            old_desc = desc_rel_path_for_source(old_source)
            new_desc = desc_rel_path_for_source(new_source)
            renamed_metadata = False
            with config.workspace_service.workspace_lock(workspace):
                old_abs = _safe_workspace_path(workspace, old_source)
                if old_abs.is_symlink() or (not old_abs.exists()) or (not old_abs.is_file()):
                    raise ValueError('solution source does not exist')
                new_abs = _safe_workspace_path(workspace, new_source)
                if new_abs.exists():
                    raise ValueError('destination source already exists')
                old_desc_exists = _workspace_rel_file_exists(workspace, old_desc)
                if old_desc_exists and _workspace_rel_file_exists(workspace, new_desc):
                    raise ValueError('destination metadata already exists')
                config.git_service.rename_path(workspace, old_source, new_source)
                if old_desc_exists and old_desc != new_desc:
                    config.git_service.rename_path(workspace, old_desc, new_desc)
                    renamed_metadata = True
                build_cfg, cfg_path = _read_build_config(workspace)
                configured = _normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
                if configured == old_source:
                    build_cfg['accepted_solution_source'] = new_source
                    _write_build_config(cfg_path, build_cfg)
            selected = new_source
            _audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.rename', {'old': old_source, 'new': new_source, 'renamed_metadata': renamed_metadata})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/solutions?path={quote_plus(selected)}', status_code=303, message=msg)

def solutions_delete(problem: str, user: str, source_path: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = _normalize_workspace_rel_path(source_path)
    msg = 'solution deleted'
    try:
        selected = _normalize_solution_source_path_required(source_path)
        desc_path = desc_rel_path_for_source(selected)
        with config.workspace_service.workspace_lock(workspace):
            source_abs = _safe_workspace_path(workspace, selected)
            if source_abs.is_symlink() or (not source_abs.exists()) or (not source_abs.is_file()):
                raise ValueError('solution source does not exist')
            config.git_service.delete_path(workspace, selected)
            if _workspace_rel_file_exists(workspace, desc_path):
                config.git_service.delete_path(workspace, desc_path)
            build_cfg, cfg_path = _read_build_config(workspace)
            configured = _normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
            if configured == selected:
                build_cfg.pop('accepted_solution_source', None)
                _write_build_config(cfg_path, build_cfg)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.delete', {'source': selected, 'desc': desc_path})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/solutions', status_code=303, message=msg)

def settings_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    is_system_admin = _is_system_admin_user_id(int(ctx['user']['id']))
    ctx['user']['is_system_admin'] = 1 if is_system_admin else 0
    problems = _user_participating_problems(int(ctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    auth_row = _lookup_user_auth(str(ctx['user']['username']))
    current_salt = str(auth_row['password_salt'] or '').strip().lower() if auth_row is not None else ''
    try:
        current_iters = int(auth_row['password_iters'] or 0) if auth_row is not None else int(_C.PASSWORD_HASH_ITERS)
    except Exception:
        current_iters = int(_C.PASSWORD_HASH_ITERS)
    if not _C.HEX_32_RE.fullmatch(current_salt):
        current_salt = _dummy_password_salt_hex(str(ctx['user']['username']))
    if current_iters <= 0:
        current_iters = int(_C.PASSWORD_HASH_ITERS)
    admin_rows: list[dict[str, object]] = []
    admin_payload_json = '{}'
    if is_system_admin:
        config.system_config_service.refresh()
        admin_rows = config.system_config_service.ui_rows()
        admin_payload_json = json.dumps(config.system_config_service.snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
    return _template_response(request, 'settings.html', {'ctx': ctx, 'problems': problems, 'password_csrf_token': _issue_password_form_csrf_token('settings-password'), 'current_password_salt': current_salt, 'current_password_iters': current_iters, 'new_password_salt': secrets.token_hex(16), 'new_password_iters': int(_C.PASSWORD_HASH_ITERS), 'is_system_admin': is_system_admin, 'admin_config_rows': admin_rows, 'admin_config_json': admin_payload_json})

def settings_system_config_update(problem: str, user: str, config_json: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_system_admin(ctx)
    msg = 'system config updated'
    try:
        raw_payload = str(config_json or '').strip()
        if not raw_payload:
            raise ValueError('system config payload is required')
        payload = json.loads(raw_payload)
        effective = config.system_config_service.update_from_payload(payload, actor_user_id=int(ctx['user']['id']))
        overrides = sum((1 for key in _C.ADMIN_CONFIG_DEFAULTS if effective.get(key) != _C.ADMIN_CONFIG_DEFAULTS[key]))
        _audit(ctx['user']['id'], ctx['problem']['id'], 'system_config.update', {'override_count': overrides})
        msg = f'system config updated ({overrides} overrides); restart service to apply runtime changes'
    except json.JSONDecodeError:
        msg = 'invalid JSON payload'
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/settings', status_code=303, message=msg)

def settings_system_config_reset(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_system_admin(ctx)
    config.system_config_service.reset()
    _audit(ctx['user']['id'], ctx['problem']['id'], 'system_config.reset', {})
    return _redirect_response(f'/problems/{problem}/{user}/settings', status_code=303, message='system config reset to defaults; restart service to apply runtime changes')

def settings_password_update(problem: str, user: str, current_password: str=Form(''), new_password: str=Form(''), new_password_confirm: str=Form(''), current_password_proof: str=Form(''), new_password_verifier: str=Form(''), new_password_proof: str=Form(''), csrf_token: str=Form(''), new_password_salt: str=Form(''), new_password_iters: str=Form('')):
    row = _lookup_user_auth(user)
    msg = 'password updated'
    response: RedirectResponse
    if row is None:
        msg = 'user not found'
        return _redirect_response(f'/problems/{problem}/{user}/settings', status_code=303, message=msg)
    try:
        raw_current_password = _form_text(current_password)
        raw_new_password = _form_text(new_password)
        raw_new_password_confirm = _form_text(new_password_confirm)
        proof_token = _form_text(csrf_token).strip()
        current_proof_value = _form_text(current_password_proof).strip().lower()
        new_verifier_value = _form_text(new_password_verifier).strip().lower()
        new_proof_value = _form_text(new_password_proof).strip().lower()
        new_salt_value = _form_text(new_password_salt)
        new_iters_value = _form_text(new_password_iters)
        verifier_mode = bool(proof_token or current_proof_value or new_verifier_value or new_proof_value)
        if verifier_mode:
            if not _verify_password_form_csrf_token(proof_token, 'settings-password'):
                raise ValueError('invalid password token')
            stored_verifier = str(row['password_hash'] or '').strip().lower()
            if not _C.HEX_64_RE.fullmatch(stored_verifier):
                raise ValueError('current password is incorrect')
            if not _C.HEX_64_RE.fullmatch(current_proof_value):
                raise ValueError('current password is incorrect')
            expected_current_proof = _password_proof_from_verifier(proof_token, stored_verifier)
            if not secrets.compare_digest(expected_current_proof, current_proof_value):
                raise ValueError('current password is incorrect')
            new_verifier = _normalize_password_verifier_hex(new_verifier_value)
            if not _C.HEX_64_RE.fullmatch(new_proof_value):
                raise ValueError('invalid new password proof')
            new_salt = _normalize_password_salt_hex(new_salt_value)
            new_iters = _normalize_password_iters(new_iters_value)
            if new_iters != int(_C.PASSWORD_HASH_ITERS):
                raise ValueError('invalid password iterations')
            expected_new_proof = _password_proof_from_verifier(proof_token, new_verifier)
            if not secrets.compare_digest(expected_new_proof, new_proof_value):
                raise ValueError('invalid new password proof')
            _set_user_password_verifier(int(row['id']), new_verifier, new_salt, new_iters)
        else:
            if not _verify_user_password(row, raw_current_password):
                raise ValueError('current password is incorrect')
            if raw_new_password != raw_new_password_confirm:
                raise ValueError('password confirmation does not match')
            _set_user_password(int(row['id']), raw_new_password)
        config.db.execute('UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', [now_iso(), int(row['id'])])
        token = _create_session_for_user(int(row['id']))
        response = _redirect_response(f'/problems/{problem}/{user}/settings', status_code=303, message=msg)
        response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
        return response
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/settings', status_code=303, message=msg)

def switch_workspace(request: Request, problem: str=Form(...), user: str=Form(''), page: str=Form('general')):
    active_user = _session_user(request) or str(user or '').strip()
    if not active_user:
        return _login_redirect(request)
    safe_problem = str(problem or '').strip()
    try:
        if not safe_problem:
            raise ValueError('problem id is required')
        user_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [active_user])
        if user_row is None:
            ensured = config.workspace_service.ensure_user(active_user)
            user_id = int(ensured['id'])
        else:
            user_id = int(user_row['id'])
        problem_row = config.db.fetch_one('SELECT id FROM problems WHERE slug=?', [safe_problem])
        if problem_row is None:
            config.workspace_service.ensure_problem(safe_problem, f'{safe_problem.title()} Problem')
            config.workspace_service.grant_repo_access(safe_problem, active_user, 'owner')
        else:
            access = _workspace_access_context(int(problem_row['id']), user_id)
            if not bool(access.get('can_read')):
                raise ValueError('you do not have access to this problem; ask an owner to grant access')
        config.workspace_service.ensure_workspace(safe_problem, active_user)
    except ValueError as exc:
        msg = str(exc)
        return _redirect_response(f'/problems/{active_user}/problems', status_code=303, message=msg)
    target_page = _normalize_page_target(page)
    return _redirect_response(f'/problems/{safe_problem}/{active_user}/{target_page}', status_code=303)

def files_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    selected = _normalize_workspace_rel_path(request.query_params.get('path'))
    line_raw = request.query_params.get('line')
    selected_line = _parse_line_param(line_raw, default=1)
    source_page = _normalize_files_source(request.query_params.get('src'))
    source_id = _normalize_source_id(request.query_params.get('sid'))
    source_back_href, source_back_label = _files_back_target(problem, user, source_page, source_id)
    source_query_tail = _files_source_query_tail(source_page, source_id)
    content = ''
    content_truncated = False
    selected_missing = False
    selected_is_dir = False
    auto_message = ''
    files, files_truncated = config.git_service.list_files_capped(workspace, limit=_C.WORKSPACE_FILE_LIST_LIMIT)
    default_selected = _default_files_selected_path(workspace, files)
    if not selected:
        selected = default_selected
    try:
        selected_abs = _safe_workspace_path(workspace, selected)
    except HTTPException:
        selected = default_selected
        selected_abs = _safe_workspace_path(workspace, selected)
        auto_message = f'invalid path; opened {selected}'
    if selected_abs.exists() and selected_abs.is_file():
        content, content_truncated = config.git_service.read_file_limited(workspace, selected, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    elif selected_abs.exists() and selected_abs.is_dir():
        selected_is_dir = True
    else:
        selected_missing = True
    selected_template_kind = _kind_for_path(selected)
    selected_parent = str(Path(selected).parent)
    if selected_parent in {'.', ''}:
        selected_parent = ''
    requested_dir = request.query_params.get('dir')
    browse_dir_default = ''
    browse_dir_raw = requested_dir if requested_dir is not None else browse_dir_default
    browse_dir, browse_parent, browse_dirs, browse_files, browse_total = _build_repo_browser_entries(workspace, files, str(browse_dir_raw or ''))
    browse_query_tail = _files_browse_query_tail(browse_dir)
    line_focus = _build_line_focus_context(content, selected_line) if line_raw else None
    line_jump_requested = bool(line_raw)
    line_jump_missing = bool(line_jump_requested and line_focus is None)
    message = ''
    if not message and auto_message:
        message = auto_message
    return _template_response(request, 'files.html', {'ctx': ctx, 'files': files, 'files_truncated': files_truncated, 'file_limit': _C.WORKSPACE_FILE_LIST_LIMIT, 'selected': selected, 'content': content, 'content_truncated': content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'selected_line': selected_line, 'selected_parent': selected_parent, 'browse_dir': browse_dir, 'browse_parent': browse_parent, 'browse_dirs': browse_dirs, 'browse_files': browse_files, 'browse_total': browse_total, 'browse_query_tail': browse_query_tail, 'line_focus': line_focus, 'line_jump_requested': line_jump_requested, 'line_jump_missing': line_jump_missing, 'selected_missing': selected_missing, 'selected_is_dir': selected_is_dir, 'selected_template_kind': selected_template_kind, 'source_page': source_page, 'source_id': source_id, 'source_query_tail': source_query_tail, 'source_back_href': source_back_href, 'source_back_label': source_back_label, 'message': message})

def files_save(problem: str, user: str, path: str=Form(...), content: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    msg = 'saved'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.write_file(workspace, path, content)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'files.save', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

def files_new(problem: str, user: str, path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    msg = 'created'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.write_file(workspace, path, '')
        _audit(ctx['user']['id'], ctx['problem']['id'], 'files.new', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

def files_create_template(problem: str, user: str, path: str=Form(...), kind: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    msg = 'template created'
    try:
        expected_kind = _kind_for_path(path)
        if not expected_kind:
            raise ValueError('template is only available for checker/interactor/validator/accepted solution')
        if expected_kind != str(kind).strip().lower():
            raise ValueError('template kind/path mismatch')
        content = _template_for_kind(kind)
        with config.workspace_service.workspace_lock(workspace):
            abs_path = _safe_workspace_path(workspace, path)
            if abs_path.exists() and abs_path.is_dir():
                raise ValueError('template target must be a file path')
            if abs_path.exists() and abs_path.is_file() and (abs_path.stat().st_size > 0):
                msg = 'file already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, path, content)
                if expected_kind == 'solution' and _ensure_solution_metadata_for_source(workspace, path):
                    msg = 'template and metadata created'
                _audit(ctx['user']['id'], ctx['problem']['id'], 'files.create_template', {'path': path, 'kind': kind})
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

async def files_upload(problem: str, user: str, path: str=Form(...), upload: UploadFile=File(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    total_bytes = 0
    tmp_path: Path | None = None
    try:
        with config.workspace_service.workspace_lock(workspace):
            abs_path = _safe_workspace_path(workspace, path)
            if abs_path.exists() and abs_path.is_dir():
                raise HTTPException(status_code=400, detail='upload target must be a file path')
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.upload-{abs_path.name}.', suffix='.tmp', dir=str(abs_path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, 'wb') as out:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        total_bytes += len(chunk)
                os.replace(tmp_path, abs_path)
                tmp_path = None
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                    tmp_path = None
                raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    _audit(ctx['user']['id'], ctx['problem']['id'], 'files.upload', {'path': path, 'bytes': total_bytes})
    return _redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message='uploaded')

def files_rename(problem: str, user: str, old_path: str=Form(...), new_path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    selected = new_path
    msg = 'renamed'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rename_path(workspace, old_path, new_path)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'files.rename', {'old': old_path, 'new': new_path})
    except (ValueError, OSError) as exc:
        selected = old_path
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(selected)}{tail}', status_code=303, message=msg)

def files_delete(problem: str, user: str, path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = _normalize_files_source(src)
    source_id = _normalize_source_id(sid)
    tail = _files_browse_query_tail(dir) + _files_source_query_tail(source_page, source_id)
    msg = 'deleted'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.delete_path(workspace, path)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'files.delete', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/files{tail}', status_code=303, message=msg)

def files_download(problem: str, user: str, path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    file_path = _safe_workspace_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail='file not found')
    return FileResponse(file_path, filename=file_path.name)

def workspace_page(request: Request, problem: str, user: str):
    return _render_workspace_page(request, problem, user)

def access_page(request: Request, problem: str, user: str):
    return _render_workspace_page(request, problem, user, show_access_admin=True)

def workspace_access_grant(problem: str, user: str, target_user: str=Form(...), role: str=Form('read')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_manage_access(ctx)
    msg = 'access updated'
    try:
        safe_target = _normalize_username_required(target_user)
        safe_role = _normalize_repo_role(role)
        target_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [safe_target])
        if target_row is None:
            raise ValueError('target user not found; ask them to register first')
        target_user_id = int(target_row['id'])
        problem_id = int(ctx['problem']['id'])
        existing = config.db.fetch_one('SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        existing_role = str(existing['role']).strip().lower() if existing is not None else ''
        if existing_role == 'owner' and safe_role != 'owner' and (_problem_owner_count(problem_id) <= 1):
            raise ValueError('cannot demote the last owner')
        config.db.execute('\n            INSERT INTO repo_acl(problem_id,user_id,role,created_at)\n            VALUES(?,?,?,?)\n            ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role\n            ', [problem_id, target_user_id, safe_role, now_iso()])
        _audit(int(ctx['user']['id']), problem_id, 'access.grant', {'target_user': safe_target, 'role': safe_role})
        msg = f'access updated: {safe_target} -> {safe_role}'
    except ValueError as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/access', status_code=303, message=msg)

def workspace_access_revoke(problem: str, user: str, target_user: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_manage_access(ctx)
    msg = 'access removed'
    redirect_to_problems = False
    try:
        safe_target = _normalize_username_required(target_user)
        target_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [safe_target])
        if target_row is None:
            raise ValueError('target user not found')
        target_user_id = int(target_row['id'])
        problem_id = int(ctx['problem']['id'])
        existing = config.db.fetch_one('SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        if existing is None:
            raise ValueError('access entry not found')
        existing_role = str(existing['role'] or '').strip().lower()
        if existing_role == 'owner' and _problem_owner_count(problem_id) <= 1:
            raise ValueError('cannot remove the last owner')
        config.db.execute('DELETE FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, target_user_id])
        redirect_to_problems = target_user_id == int(ctx['user']['id'])
        _audit(int(ctx['user']['id']), problem_id, 'access.revoke', {'target_user': safe_target})
        msg = f'access removed: {safe_target}'
    except ValueError as exc:
        msg = str(exc)
    if redirect_to_problems:
        return _redirect_response(f'/problems/{user}/problems', status_code=303, message=msg)
    return _redirect_response(f'/problems/{problem}/{user}/access', status_code=303, message=msg)

def history_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    commits: list[dict] = []
    message = ''
    try:
        commits = config.git_service.history(workspace, limit=_C.WORKSPACE_HISTORY_LIMIT)
        revision_top = int(ctx['workspace_version']) if ctx.get('workspace_version') is not None else None
        for idx, row in enumerate(commits):
            if revision_top is None:
                row['version'] = None
            else:
                row['version'] = max(1, revision_top - idx)
    except Exception as exc:
        if not message:
            message = str(exc)
    return _template_response(request, 'history.html', {'ctx': ctx, 'commits': commits, 'message': message})

def git_commit(problem: str, user: str, message: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
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
            _audit(ctx['user']['id'], ctx['problem']['id'], 'git.commit', {'message': message, 'head': commit_head})
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.push', {'branch': 'main', 'via': 'commit'})
        msg = 'commit and publish ok' if commit_created else 'publish ok'
    except Exception as exc:
        err = str(exc)
        err_lower = err.lower()
        if 'non-fast-forward' in err_lower or 'fetch first' in err_lower or 'rejected' in err_lower:
            msg = 'publish failed: upstream advanced; rebase required, commit rolled back'
        else:
            msg = err
    return _redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_push(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.push(workspace, 'main')
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.push', {'branch': 'main'})
        msg = 'push ok'
    except Exception as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_pull(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.pull(workspace, 'main')
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.pull', {'branch': 'main'})
        msg = 'pull ok'
    except Exception as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_restore_revision(problem: str, user: str, revision: str=Form(...), page: str=Form('history')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    target_page = 'workspace' if str(page or '').strip().lower() == 'workspace' else 'history'
    try:
        with config.workspace_service.workspace_lock(workspace):
            resolved = config.git_service.restore_revision_to_working_copy(workspace, revision)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.restore_revision', {'revision': revision, 'resolved_commit': resolved})
        msg = f'restored files from {resolved[:12]} on top of latest main; commit when ready'
    except Exception as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=msg)

def git_rebase_continue(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rebase_continue(workspace)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.rebase_continue', {})
        msg = 'rebase continue ok'
    except Exception as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)

def git_rebase_abort(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rebase_abort(workspace)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'git.rebase_abort', {})
        msg = 'rebase aborted'
    except Exception as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/workspace', status_code=303, message=msg)
