from __future__ import annotations
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import File, Form, UploadFile
from fastapi.responses import FileResponse
from fastapi import HTTPException, Request
from app.impl.auth import (
    _redirect_response,
    _template_response,
)
from app.impl.config import config
from app.main_utils import _normalize_optional_component_source_path, _normalize_optional_component_source_path_safe
from app.services.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior

from app.impl.workspace import (
    _allocate_invocation_id,
    _allocate_run_id,
    _assert_workspace_artifact_access,
    _assert_workspace_build_access,
    _audit,
    _browser_file_response,
    _cleanup_runtime_cache,
    _dedupe_preserve_order,
    _export_download_filename,
    _git_commit_count,
    _latest_workspace_build,
    _latest_workspace_committed_build,
    _normalize_problem_mode,
    _parse_run_detail_ids,
    _parse_run_detail_invocation_id,
    _parse_run_test_names,
    _read_problem_config,
    _require_write_access,
    _run_invocation_scope_run_ids,
    _run_list_rows,
    _run_solution_options_context,
    _run_test_options_context,
    _solution_compile_check_error,
    _upload_compile_check_error,
    _safe_artifact_path,
    _safe_run_artifact_path,
    _start_export_job,
    _start_run_execute_batch,
    _build_run_detail_context,
    page_ctx,
)

_C = config.constants

def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    execute_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    workspace_id = int(ctx['workspace']['id'])
    requested_invocation_id = _parse_run_detail_invocation_id(request)
    requested_detail_ids = _parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = _run_invocation_scope_run_ids(int(ctx['problem']['id']), workspace_id, int(ctx['user']['id']), requested_invocation_id)
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids
    if requested_invocation_id or requested_detail_ids:
        detail_ctx = _build_run_detail_context(ctx, detail_scope_run_ids, execute_mode, allow_latest_fallback=False)
        detail_page_ctx = dict(ctx)
        detail_page_ctx['page_wide_content'] = bool(detail_ctx.get('detail_columns'))
        return _template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})
    runs = _run_list_rows(int(ctx['problem']['id']), workspace_id, workspace, limit=40, actor_user_id=int(ctx['user']['id']))
    return _template_response(request, 'run.html', {'ctx': ctx, 'runs': runs})

def run_new_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    active_build = _latest_workspace_build(int(ctx['problem']['id']), workspace_id, ok_only=True)
    solution_options, default_submission_path, solution_options_truncated = _run_solution_options_context(workspace)
    test_options, test_options_truncated, test_options_source = _run_test_options_context(problem, workspace, active_build)
    selected_solution_paths: list[str] = []
    for raw in request.query_params.getlist('solution_paths'):
        normalized = _normalize_optional_component_source_path_safe(raw, 'solutions', 'solution path')
        if normalized:
            selected_solution_paths.append(normalized)
    selected_solution_paths = _dedupe_preserve_order(selected_solution_paths)
    if not selected_solution_paths and default_submission_path:
        selected_solution_paths = [default_submission_path]
    selected_test_names_raw = request.query_params.getlist('test_names')
    selected_test_names = _parse_run_test_names(selected_test_names_raw)
    allowed_test_names = {str(row.get('name') or '') for row in test_options}
    selected_test_names = [name for name in selected_test_names if name in allowed_test_names]
    if not selected_test_names_raw and test_options and (not test_options_truncated):
        selected_test_names = [str(row.get('name') or '') for row in test_options if str(row.get('name') or '').strip()]
    return _template_response(request, 'run_execute.html', {'ctx': ctx, 'solution_options': solution_options, 'solution_options_truncated': solution_options_truncated, 'selected_solution_paths': selected_solution_paths, 'test_options': test_options, 'test_options_truncated': test_options_truncated, 'test_options_source': test_options_source, 'selected_test_names': selected_test_names})

def run_details_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    execute_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    requested_invocation_id = _parse_run_detail_invocation_id(request)
    requested_detail_ids = _parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    allow_latest_fallback = True
    if requested_invocation_id:
        allow_latest_fallback = False
        detail_scope_run_ids = _run_invocation_scope_run_ids(int(ctx['problem']['id']), int(ctx['workspace']['id']), int(ctx['user']['id']), requested_invocation_id)
    elif requested_detail_ids:
        allow_latest_fallback = False
        detail_scope_run_ids = requested_detail_ids
    detail_ctx = _build_run_detail_context(ctx, detail_scope_run_ids, execute_mode, allow_latest_fallback=allow_latest_fallback)
    detail_page_ctx = dict(ctx)
    detail_page_ctx['page_wide_content'] = bool(detail_ctx.get('detail_columns'))
    return _template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})

def run_execute(problem: str, user: str, build_id: str=Form(''), solution_paths: list[str]=Form(default=[]), test_names: list[str]=Form(default=[]), submission_upload: UploadFile | None=File(None)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = _read_problem_config(workspace)
    run_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    solution_options, _, _ = _run_solution_options_context(workspace)
    solution_expected_map: dict[str, str] = {}
    for row in solution_options:
        path = str(row.get('path') or '').strip()
        if not path:
            continue
        solution_expected_map[path] = normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))
    upload_content = None
    upload_filename = ''
    uploaded = False
    try:
        if submission_upload is not None:
            normalized_name = (submission_upload.filename or '').strip()
            if normalized_name:
                upload_filename = normalized_name
                upload_content = submission_upload.file.read()
                uploaded = True
        raw_solution_paths: list[str] = []
        if isinstance(solution_paths, str):
            raw_solution_paths.append(solution_paths)
        elif isinstance(solution_paths, list):
            raw_solution_paths.extend([str(item or '') for item in solution_paths])
        elif solution_paths:
            raw_solution_paths.extend([str(item or '') for item in list(solution_paths)])
        selected_solution_paths: list[str] = []
        for raw in raw_solution_paths:
            token = str(raw or '').strip()
            if not token:
                continue
            selected_solution_paths.append(_normalize_optional_component_source_path(token, 'solutions', 'solution path'))
        selected_solution_paths = _dedupe_preserve_order(selected_solution_paths)
        selected_test_names = _parse_run_test_names(test_names)
        execution_targets: list[tuple[str | None, bool]] = []
        execution_targets.extend(((path, False) for path in selected_solution_paths))
        if uploaded:
            execution_targets.append((None, True))
        if not execution_targets:
            msg = 'select at least one solution or upload source file'
            return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        deduped_targets: list[tuple[str | None, bool]] = []
        seen_targets: set[tuple[str, bool]] = set()
        for target_submission_path, target_is_upload in execution_targets:
            key = (str(target_submission_path or ''), bool(target_is_upload))
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped_targets.append((target_submission_path, target_is_upload))
        execution_targets = deduped_targets
        if uploaded and isinstance(upload_content, (bytes, bytearray)):
            compile_check_error = _upload_compile_check_error(workspace, upload_filename, bytes(upload_content))
            if compile_check_error:
                msg = f'upload compile check failed: {compile_check_error}'
                return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        for target_submission_path, target_is_upload in execution_targets:
            if target_is_upload:
                continue
            solution_path = str(target_submission_path or '').strip()
            if not solution_path:
                continue
            compile_check_error = _solution_compile_check_error(workspace, solution_path)
            if compile_check_error:
                msg = f'compile check failed: {compile_check_error}'
                return _redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        requested_build_id = str(build_id or '').strip()
        if requested_build_id:
            _assert_workspace_build_access(ctx, requested_build_id)
        run_ids: list[str] = []
        resolved_submission_paths: list[str] = []
        background_targets: list[dict[str, object]] = []
        invocation_id = _allocate_invocation_id()
        for target_submission_path, target_is_upload in execution_targets:
            run_id = _allocate_run_id()
            run_ids.append(run_id)
            source_label = target_submission_path or upload_filename or 'upload'
            expected_behavior = 'unknown'
            if target_submission_path:
                resolved_submission_paths.append(target_submission_path)
                expected_behavior = solution_expected_map.get(target_submission_path, 'unknown')
                if expected_behavior == 'unknown':
                    safe_solution = _normalize_optional_component_source_path_safe(target_submission_path, 'solutions', 'solution path')
                    if safe_solution:
                        expected_behavior = infer_expected_behavior_from_name(safe_solution)
            background_targets.append({'run_id': run_id, 'submission_path': target_submission_path or '', 'upload_content': upload_content if target_is_upload else None, 'upload_filename': upload_filename if target_is_upload else '', 'source_label': source_label, 'expected_behavior': normalize_expected_behavior(expected_behavior)})
        primary_run_id = run_ids[0] if run_ids else ''
        run_execute_details: dict[str, object] = {
            'invocation_id': invocation_id,
            'run_id': primary_run_id,
            'run_ids': run_ids,
            'run_count': len(run_ids),
            'build_id': requested_build_id,
            'submission_paths': resolved_submission_paths,
            'solution_paths': selected_solution_paths,
            'selected_test_names': selected_test_names,
            'uploaded': uploaded,
            'mode': run_mode,
            'implicit_build_generated': not bool(requested_build_id),
            'invocation_backend': config.invocation_backend_service.active_backend_name(),
            'async': True,
            'status': 'queued',
        }
        _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', run_execute_details)
        try:
            started = _start_run_execute_batch(problem, user, requested_build_id=requested_build_id, run_mode=run_mode, targets=background_targets, invocation_id=invocation_id, invocation_run_ids=run_ids, selected_test_names=selected_test_names)
        except Exception as exc:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = str(exc)
            _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=str(exc))
        if not started:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = 'invocation queue rejected'
            _audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message='invocation queue rejected')
        message_parts: list[str] = []
        if selected_test_names:
            message_parts.append(f'tests selected ({len(selected_test_names)})')
        message_parts.append(f'invocation running ({len(run_ids)} programs)')
        message_text = '; '.join(message_parts)
        if primary_run_id:
            details_query: list[str] = []
            details_query.append(f'invocation_id={quote_plus(invocation_id)}')
            return _redirect_response(f'/problems/{problem}/{user}/run/details?{'&'.join(details_query)}', status_code=303, message=message_text)
        return _redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=message_text)
    finally:
        if submission_upload is not None:
            submission_upload.file.close()

def export_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace = Path(ctx['workspace']['path'])
    generate_revision: int | None = _git_commit_count(workspace, head_commit) if head_commit else None
    generate_revision_display = f'v{generate_revision}' if isinstance(generate_revision, int) and generate_revision >= 0 else 'missing'
    active_build = _latest_workspace_committed_build(problem_id, int(workspace_id), head_commit, ok_only=True)
    build_status = 'ready' if active_build is not None else 'missing'
    build_note = ''
    if not head_commit:
        build_note = 'no committed revision yet; commit changes before generating package'
    elif active_build is None:
        build_note = 'no committed tests snapshot for this revision; Generate will build from committed revision'
    else:
        build_note = 'committed revision tests are ready for export'
    exports_rows = config.db.fetch_all('\n        SELECT id,build_id,export_type,filename,sha256,size_bytes,source_commit,created_at\n        FROM exports\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT 40\n        ', [ctx['problem']['id'], workspace_id])
    revision_cache: dict[str, int | None] = {}
    exports: list[dict[str, object]] = []
    for row in exports_rows:
        item = dict(row)
        source_commit = str(item.get('source_commit') or '').strip()
        revision = None
        if source_commit:
            if source_commit in revision_cache:
                revision = revision_cache[source_commit]
            else:
                revision = _git_commit_count(workspace, source_commit)
                revision_cache[source_commit] = revision
        item['revision'] = revision
        item['revision_display'] = f'v{revision}' if isinstance(revision, int) and revision >= 0 else 'v?'
        item['display_filename'] = f'{ctx['problem']['slug']}-{item['revision_display']}.zip'
        exports.append(item)
    return _template_response(request, 'export.html', {'ctx': ctx, 'active_build': active_build, 'build_status': build_status, 'build_note': build_note, 'generate_revision_display': generate_revision_display, 'exports': exports})

def export_create(problem: str, user: str, build_id: str=Form(''), export_type: str=Form('icpc')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    _require_write_access(ctx)
    resolved_build_id = str(build_id or '').strip()
    requested_export_type = str(export_type or '').strip().lower()
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
    if not requested_export_type:
        requested_export_type = 'icpc'
    initial_details: dict[str, object] = {'status': 'running', 'build_id': resolved_build_id, 'export_type': requested_export_type, 'source_commit': head_commit, 'filename': '', 'error': ''}
    try:
        if requested_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        if not head_commit:
            raise ValueError('no committed revision; commit changes first')
        started = _start_export_job(problem, user, actor_user_id=int(ctx['user']['id']), problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_build_id=resolved_build_id, requested_export_type=requested_export_type, initial_details=initial_details)
        msg = 'package generation queued' if started else 'package generation already running for this revision'
    except ValueError as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    except Exception as exc:
        initial_details['status'] = 'failed'
        initial_details['error'] = str(exc)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'export.create', initial_details)
        msg = str(exc)
    _cleanup_runtime_cache(force=False)
    return _redirect_response(f'/problems/{problem}/{user}/export', status_code=303, message=msg)

def artifact_file(problem: str, user: str, build_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _assert_workspace_artifact_access(ctx, build_id)
    file_path = _safe_artifact_path(problem, build_id, rel_path)
    rel_norm = str(rel_path or '').lstrip('/')
    if rel_norm.startswith('export/'):
        export_name = Path(rel_norm).name
        download_name = _export_download_filename(ctx, build_id, export_name)
        if download_name:
            return FileResponse(file_path, filename=download_name)
    return _browser_file_response(file_path)

def run_artifact_file(problem: str, user: str, run_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    rel_norm = str(rel_path or '').strip().lstrip('/')
    if Path(rel_norm).name == 'compile.log':
        raise HTTPException(status_code=403, detail='compile.log download is disabled')
    file_path = _safe_run_artifact_path(ctx, run_id, rel_path)
    return _browser_file_response(file_path)
