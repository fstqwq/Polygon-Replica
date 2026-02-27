from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from app.impl.auth import (
    _redirect_response,
    _template_response,
)
from app.impl.config import config
from app.main_utils import (
    _safe_workspace_path,
    _sanitize_log_text_for_ui,
)
from app.services.solution_metadata import normalize_expected_behavior
from app.services.statement_template import (
    DEFAULT_STATEMENT_CONTENT,
    STATEMENT_CONTENT_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    render_statement_main,
    statement_sources_signature,
)
from app.services.tests_spec import (
    TESTS_SPEC_MANUAL_MAX_CHARS,
    TESTS_SPEC_REL,
    next_test_id,
    normalize_gen_command,
    normalize_manual_input,
    normalize_test_id,
    normalize_test_kind,
)

from app.impl.workspace import (
    _allocate_invocation_id,
    _allocate_run_id,
    _artifact_root,
    _audit,
    _cleanup_runtime_cache,
    _normalize_page_target,
    _parse_gen_batch_items,
    _parse_manual_batch_items,
    _parse_summary_json,
    _read_tests_spec,
    _read_text_safe_limited,
    _read_workspace_source_with_default,
    _require_write_access,
    _run_solution_options_context,
    _safe_artifact_path,
    _start_verification_job,
    _tests_spec_bool_flag,
    _tests_spec_editor_context,
    _tests_spec_form_text,
    _tests_spec_payload_file_path,
    _tests_spec_read_payload,
    _tests_spec_remove_payload,
    _tests_spec_resolve_index,
    _tests_spec_write_payload,
    _workspace_rel_file_exists,
    _write_tests_spec,
    page_ctx,
)

_C = config.constants

def build_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    tests_editor_error = ''
    try:
        tests_editor = _tests_spec_editor_context(workspace)
    except ValueError as exc:
        tests_editor_error = str(exc)
        tests_editor = {'path': TESTS_SPEC_REL.as_posix(), 'exists': False, 'entries': [], 'rows': [], 'summary': {'total': 0, 'manual': 0, 'gen': 0}, 'total': 0, 'shown': 0, 'truncated': False}
    if tests_editor_error:
        return _template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor, 'message': tests_editor_error})
    return _template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor})

def tests_spec_add_manual(problem: str, user: str, test_id: str=Form(''), sample: str=Form('0'), manual_input: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'manual test added'
    try:
        safe_input = normalize_manual_input(_tests_spec_form_text(manual_input))
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        requested_id = _tests_spec_form_text(test_id).strip()
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            safe_test_id = normalize_test_id(requested_id) if requested_id else next_test_id(entries)
            if any((str(row.get('id') or '').strip() == safe_test_id for row in entries)):
                raise ValueError(f'test id already exists: {safe_test_id}')
            entries.append({'id': safe_test_id, 'kind': 'manual', 'sample': safe_sample})
            _write_tests_spec(spec_path, entries)
            _tests_spec_write_payload(workspace, safe_test_id, 'manual', safe_input)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.add_manual', {'index': len(entries), 'id': safe_test_id, 'sample': safe_sample})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_add_manual_batch(problem: str, user: str, manual_batch_text: str=Form(''), desc_prefix: str=Form(''), sample: str=Form('0')):
    _ = desc_prefix
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'manual tests added'
    try:
        batch_items = _parse_manual_batch_items(_tests_spec_form_text(manual_batch_text))
        if not batch_items:
            raise ValueError('manual batch is empty')
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            base = len(entries)
            for item in batch_items:
                safe_test_id = next_test_id(entries)
                entries.append({'id': safe_test_id, 'kind': 'manual', 'sample': safe_sample})
                _tests_spec_write_payload(workspace, safe_test_id, 'manual', item)
            _write_tests_spec(spec_path, entries)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.add_manual_batch', {'count': len(batch_items), 'start_index': base + 1, 'sample': safe_sample})
        msg = f'manual tests added: {len(batch_items)}'
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_add_gen(problem: str, user: str, test_id: str=Form(''), sample: str=Form('0'), command: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator test added'
    try:
        safe_command = normalize_gen_command(_tests_spec_form_text(command))
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        requested_id = _tests_spec_form_text(test_id).strip()
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            safe_test_id = normalize_test_id(requested_id) if requested_id else next_test_id(entries)
            if any((str(row.get('id') or '').strip() == safe_test_id for row in entries)):
                raise ValueError(f'test id already exists: {safe_test_id}')
            entries.append({'id': safe_test_id, 'kind': 'gen', 'sample': safe_sample})
            _write_tests_spec(spec_path, entries)
            _tests_spec_write_payload(workspace, safe_test_id, 'gen', safe_command)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.add_gen', {'index': len(entries), 'id': safe_test_id, 'sample': safe_sample, 'command': safe_command})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_add_gen_batch(problem: str, user: str, gen_batch_text: str=Form(''), desc_prefix: str=Form(''), sample: str=Form('0')):
    _ = desc_prefix
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator tests added'
    try:
        batch_items = _parse_gen_batch_items(_tests_spec_form_text(gen_batch_text))
        if not batch_items:
            raise ValueError('generator batch is empty')
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            base = len(entries)
            for cmd in batch_items:
                safe_test_id = next_test_id(entries)
                entries.append({'id': safe_test_id, 'kind': 'gen', 'sample': safe_sample})
                _tests_spec_write_payload(workspace, safe_test_id, 'gen', cmd)
            _write_tests_spec(spec_path, entries)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.add_gen_batch', {'count': len(batch_items), 'start_index': base + 1, 'sample': safe_sample})
        msg = f'generator tests added: {len(batch_items)}'
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_update(problem: str, user: str, index: str=Form(...), kind: str=Form(''), sample: str=Form('0'), payload: str=Form(''), manual_input: str=Form(''), command: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test updated'
    try:
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            current = entries[idx - 1]
            current_kind = str(current.get('kind') or '').strip().lower()
            safe_kind = _tests_spec_form_text(kind).strip().lower() or current_kind
            if safe_kind not in {'manual', 'gen'}:
                raise ValueError('invalid test kind')
            safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
            test_id = normalize_test_id(current.get('id'))
            submitted_payload = _tests_spec_form_text(payload)
            if not str(submitted_payload):
                submitted_payload = _tests_spec_form_text(manual_input) if safe_kind == 'manual' else _tests_spec_form_text(command)
            if not str(submitted_payload):
                submitted_payload = _tests_spec_read_payload(workspace, current)
            if safe_kind == 'manual':
                safe_payload = normalize_manual_input(submitted_payload)
            elif safe_kind == 'gen':
                safe_payload = normalize_gen_command(submitted_payload)
            else:
                raise ValueError('invalid test kind')
            entries[idx - 1] = {'id': test_id, 'kind': safe_kind, 'sample': safe_sample}
            _tests_spec_write_payload(workspace, test_id, safe_kind, safe_payload)
            _write_tests_spec(spec_path, entries)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.update', {'index': idx, 'kind': safe_kind, 'sample': safe_sample, 'id': test_id})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_set_id(problem: str, user: str, index: str=Form(...), test_id: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test id updated'
    try:
        safe_new_id = normalize_test_id(_tests_spec_form_text(test_id))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            current = dict(entries[idx - 1])
            safe_old_id = normalize_test_id(current.get('id'))
            if safe_new_id != safe_old_id and any((str(row.get('id') or '').strip() == safe_new_id for i, row in enumerate(entries) if i != idx - 1)):
                raise ValueError(f'test id already exists: {safe_new_id}')
            current['id'] = safe_new_id
            current['sample'] = bool(current.get('sample'))
            current['kind'] = str(current.get('kind') or '').strip().lower()
            if current['kind'] not in {'manual', 'gen'}:
                raise ValueError('invalid test kind')
            old_payload = _tests_spec_read_payload(workspace, {**current, 'id': safe_old_id})
            entries[idx - 1] = {'id': safe_new_id, 'kind': current['kind'], 'sample': current['sample']}
            _write_tests_spec(spec_path, entries)
            _tests_spec_write_payload(workspace, safe_new_id, current['kind'], old_payload)
            if safe_new_id != safe_old_id:
                _tests_spec_remove_payload(workspace, safe_old_id)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.set_id', {'index': idx, 'old_id': safe_old_id, 'new_id': safe_new_id})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_delete(problem: str, user: str, index: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test deleted'
    try:
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            deleted = entries.pop(idx - 1)
            deleted_id = str(deleted.get('id') or '').strip()
            _write_tests_spec(spec_path, entries)
            if deleted_id:
                _tests_spec_remove_payload(workspace, deleted_id)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.delete', {'index': idx, 'kind': str(deleted.get('kind') or ''), 'id': deleted_id})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_move(problem: str, user: str, index: str=Form(...), direction: str=Form('up')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test moved'
    safe_direction = str(direction or 'up').strip().lower()
    if safe_direction not in {'up', 'down'}:
        safe_direction = 'up'
    try:
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            target = idx - 2 if safe_direction == 'up' else idx
            if target < 0 or target >= len(entries):
                raise ValueError('cannot move test further')
            entries[idx - 1], entries[target] = (entries[target], entries[idx - 1])
            _write_tests_spec(spec_path, entries)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.move', {'index': idx, 'direction': safe_direction})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_payload_download(problem: str, user: str, index: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    with config.workspace_service.workspace_lock(workspace):
        entries, _spec_path = _read_tests_spec(workspace)
        idx = _tests_spec_resolve_index(index, len(entries))
        entry = dict(entries[idx - 1])
        test_id = normalize_test_id(entry.get('id'))
        kind = normalize_test_kind(entry.get('kind'))
        payload_path: Path | None = None
        try:
            payload_path = _tests_spec_payload_file_path(workspace, test_id, kind)
        except (HTTPException, ValueError):
            payload_path = None
        if payload_path is not None:
            try:
                if payload_path.exists() and payload_path.is_file() and (not payload_path.is_symlink()):
                    return FileResponse(payload_path, filename=f'{test_id}.in', media_type='text/plain; charset=utf-8')
            except OSError:
                pass
        payload_text = _tests_spec_read_payload(workspace, entry)
    fd, tmp_name = tempfile.mkstemp(prefix=f'test-{test_id}-', suffix='.in')
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.write_text(payload_text, encoding='utf-8')
    return FileResponse(tmp_path, filename=f'{test_id}.in', media_type='text/plain; charset=utf-8', background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)))

async def tests_spec_payload_upload(problem: str, user: str, index: str=Form(...), payload_upload: UploadFile=File(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test payload uploaded'
    try:
        max_raw_bytes = max(4096, int(TESTS_SPEC_MANUAL_MAX_CHARS) * 4)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await payload_upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_raw_bytes:
                raise ValueError('uploaded payload is too large')
            chunks.append(chunk)
        raw_payload = b''.join(chunks)
        try:
            uploaded_text = raw_payload.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('uploaded payload must be utf-8 text') from exc
        safe_payload = normalize_manual_input(uploaded_text)
        with config.workspace_service.workspace_lock(workspace):
            entries, _spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            entry = dict(entries[idx - 1])
            test_id = normalize_test_id(entry.get('id'))
            kind = normalize_test_kind(entry.get('kind'))
            if kind != 'manual':
                raise ValueError('payload upload is only available for manual tests')
            _tests_spec_write_payload(workspace, test_id, 'manual', safe_payload)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.payload.upload', {'index': idx, 'id': test_id, 'bytes': len(safe_payload.encode('utf-8', errors='replace'))})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    finally:
        try:
            await payload_upload.close()
        except Exception:
            pass
    return _redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def preview_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace_id = ctx['workspace']['id']
    problem_id = int(ctx['problem']['id'])
    workspace = Path(ctx['workspace']['path'])
    problem_title = str(ctx['problem'].get('name') or '').strip()
    current_statement_signature = statement_sources_signature(workspace, problem_title=problem_title)
    requested_preview_id = str(request.query_params.get('preview_id', '') or '').strip()
    preview_id = requested_preview_id
    message = ''
    preview_rows_sql = 'SELECT id,status,source_commit,source_ref,summary_json,created_at,finished_at FROM previews WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 30'
    previews = config.db.fetch_all(preview_rows_sql, [problem_id, workspace_id])

    def _preview_has_visible_output(candidate_id: str) -> bool:
        if not candidate_id:
            return False
        try:
            _artifact_root(problem, candidate_id)
        except HTTPException:
            return False
        try:
            _safe_artifact_path(problem, candidate_id, 'statement_preview/statement.pdf')
            return True
        except HTTPException:
            pass
        try:
            _safe_artifact_path(problem, candidate_id, 'logs/latex.log')
            return True
        except HTTPException:
            return False
    if not preview_id:
        head_commit = str(ctx['workspace'].get('head_commit') or '').strip()
        dirty = bool(ctx['workspace'].get('dirty'))
        if head_commit and (not dirty):
            cached_id = config.preview_service.find_cached_preview_id(problem, problem_id, workspace_id, source_commit=head_commit, statement_signature=current_statement_signature)
            if cached_id:
                config.preview_service.prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                preview_id = cached_id
                previews = config.db.fetch_all(preview_rows_sql, [problem_id, workspace_id])
        elif dirty:
            cached_id = config.preview_service.find_cached_preview_id(problem, problem_id, workspace_id, source_commit=None, statement_signature=current_statement_signature)
            if cached_id:
                config.preview_service.prune_workspace_preview_history(problem, problem_id, workspace_id, cached_id)
                preview_id = cached_id
                previews = config.db.fetch_all(preview_rows_sql, [problem_id, workspace_id])
    if preview_id and (not _preview_has_visible_output(preview_id)):
        preview_id = ''
    content_tex, content_truncated = _read_workspace_source_with_default(workspace, STATEMENT_CONTENT_REL, DEFAULT_STATEMENT_CONTENT)
    log = ''
    log_truncated = False
    pdf_exists = False
    log_refs = []
    log_refs_total = 0
    log_refs_truncated = False
    selected_preview_nav: dict[str, object] | None = None

    def _selected_preview_nav_status(candidate_id: str) -> dict[str, object]:
        safe_id = str(candidate_id or '').strip()
        if not safe_id:
            return {'text': 'none', 'danger': True, 'warn': False}
        row = config.db.fetch_one(
            'SELECT status,source_commit,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?',
            [safe_id, problem_id, workspace_id],
        )
        if row is None:
            return {'text': 'missing', 'danger': True, 'warn': False}
        preview_status = str(row['status'] or 'none').strip().lower()
        preview_text = preview_status
        preview_danger = preview_status in {'none', 'missing', 'failed', 'error'}
        preview_warn = False
        if preview_status == 'ok':
            has_pdf_output = False
            try:
                _safe_artifact_path(problem, safe_id, 'statement_preview/statement.pdf')
                has_pdf_output = True
            except HTTPException:
                has_pdf_output = False
            if not has_pdf_output:
                return {'text': 'missing', 'danger': True, 'warn': False}
            summary_obj = _parse_summary_json(row['summary_json'], f'preview/{safe_id}') or {}
            preview_signature = str(summary_obj.get('statement_signature') or '').strip() if isinstance(summary_obj, dict) else ''
            preview_source_commit = str(row['source_commit'] or '').strip()
            workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
            stale_by_signature = bool(preview_signature and current_statement_signature and (preview_signature != current_statement_signature))
            stale_by_head = bool((not preview_signature or not current_statement_signature) and preview_source_commit and workspace_head and (preview_source_commit != workspace_head))
            if stale_by_signature or stale_by_head:
                preview_text = 'stale'
                preview_danger = False
                preview_warn = True
            else:
                preview_text = 'ok'
                preview_danger = False
        return {'text': preview_text, 'danger': preview_danger, 'warn': preview_warn}

    if preview_id:
        preview_row = config.db.fetch_one('SELECT id,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [preview_id, problem_id, workspace_id])
        if preview_row is None:
            preview_id = ''
        else:
            summary_obj = _parse_summary_json(preview_row['summary_json'], f'preview/{preview_id}') or {}
            preview_signature = str(summary_obj.get('statement_signature') or '').strip()
            if (not requested_preview_id) and (preview_signature != current_statement_signature):
                preview_id = ''
    if preview_id:
        try:
            _safe_artifact_path(problem, preview_id, 'statement_preview/statement.pdf')
            pdf_exists = True
        except HTTPException:
            pdf_exists = False
        try:
            lp = _safe_artifact_path(problem, preview_id, 'logs/latex.log')
        except HTTPException:
            lp = None
        if lp is not None:
            raw_log, log_truncated = _read_text_safe_limited(lp, _C.UI_LOG_TEXT_CHAR_LIMIT)
            redact_prefixes: list[tuple[str, str]] = [(str(workspace.resolve()), '.'), (str(config.settings.workspace_root.resolve()), '__workspace_root__'), (str(config.settings.artifacts_root.resolve()), '__artifacts__'), (str(config.settings.run_root.resolve()), '__runs__'), (str(config.settings.cache_root.resolve()), '__cache__')]
            log = _sanitize_log_text_for_ui(raw_log, path_prefixes=redact_prefixes)
            tex_ref = re.compile('(?P<file>[\\w./-]+\\.tex):(?P<line>\\d+)')
            for line in log.splitlines():
                m = tex_ref.search(line)
                if m:
                    log_refs_total += 1
                    if len(log_refs) >= _C.PREVIEW_LOG_REF_LIST_LIMIT:
                        log_refs_truncated = True
                        continue
                    log_refs.append({'file': m.group('file'), 'line': int(m.group('line')), 'context': line})
        selected_preview_nav = _selected_preview_nav_status(preview_id)
    if selected_preview_nav is not None and isinstance(ctx.get('nav_status'), dict):
        ctx['nav_status']['preview'] = selected_preview_nav
    request_path = str(getattr(request.url, 'path', '') or '')
    return_page = 'general' if request_path.endswith('/general') else 'preview'
    return _template_response(request, 'preview.html', {'ctx': ctx, 'message': message, 'preview_id': preview_id, 'previews': previews, 'content_tex': content_tex, 'content_truncated': content_truncated, 'statement_template_path': STATEMENT_TEMPLATE_REL.as_posix(), 'statement_content_path': STATEMENT_CONTENT_REL.as_posix(), 'statement_style_path': STATEMENT_STYLE_REL.as_posix(), 'editor_char_limit': _C.STATEMENT_EDITOR_CHAR_LIMIT, 'log': log, 'log_truncated': log_truncated, 'log_char_limit': _C.UI_LOG_TEXT_CHAR_LIMIT, 'pdf_exists': pdf_exists, 'log_refs': log_refs, 'log_refs_total': log_refs_total, 'log_refs_truncated': log_refs_truncated, 'log_refs_limit': _C.PREVIEW_LOG_REF_LIST_LIMIT, 'problem_name_max_len': _C.PROBLEM_NAME_MAX_LEN, 'problem_mode_values': list(_C.GENERAL_MODE_VALUES), 'time_limit_min_ms': _C.GENERAL_TIME_LIMIT_MIN_MS, 'time_limit_max_ms': _C.GENERAL_TIME_LIMIT_MAX_MS, 'memory_limit_min_mb': _C.GENERAL_MEMORY_LIMIT_MIN_MB, 'memory_limit_max_mb': _C.GENERAL_MEMORY_LIMIT_MAX_MB, 'return_page': return_page})

def preview_run(problem: str, user: str, page: str=Form('preview')):
    target_page = _normalize_page_target(page)
    if target_page in {'problems', 'contests'}:
        target_page = 'general'
    elif target_page not in {'general', 'preview'}:
        target_page = 'preview'
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    details: dict[str, object] = {
        'status': 'failed',
        'preview_id': '',
        'preview_status': 'missing',
        'workspace_head': workspace_head,
        'workspace_dirty': workspace_dirty,
        'source': 'sync',
        'source_commit': '',
        'source_ref': '',
        'error': '',
    }
    try:
        preview_id = str(config.preview_service.compile_preview(problem, user) or '').strip()
        details['preview_id'] = preview_id
        row = config.db.fetch_one('SELECT status,source_commit,source_ref,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [preview_id, problem_id, workspace_id])
        if row is None:
            raise RuntimeError('preview metadata missing after compile')
        preview_status = str(row['status'] or 'missing').strip().lower()
        details['preview_status'] = preview_status
        details['source_commit'] = str(row['source_commit'] or '').strip()
        details['source_ref'] = str(row['source_ref'] or '').strip()
        summary_obj = _parse_summary_json(row['summary_json'], f'preview/{preview_id}')
        if preview_status == 'ok':
            details['status'] = 'ok'
            _audit(ctx['user']['id'], ctx['problem']['id'], 'preview.run', details)
            _cleanup_runtime_cache(force=False)
            return _redirect_response(f'/problems/{problem}/{user}/{target_page}?preview_id={preview_id}', status_code=303)
        details['status'] = 'failed'
        details['error'] = str(summary_obj.get('error') or 'preview failed') if isinstance(summary_obj, dict) else 'preview failed'
        _audit(ctx['user']['id'], ctx['problem']['id'], 'preview.run', details)
        _cleanup_runtime_cache(force=False)
        if preview_id:
            return _redirect_response(f'/problems/{problem}/{user}/{target_page}?preview_id={preview_id}', status_code=303, message=str(details['error']))
        return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=str(details['error']))
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'preview.run', details)
    _cleanup_runtime_cache(force=False)
    return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=str(details['error']))

def preview_save(problem: str, user: str, content_tex: str=Form(''), page: str=Form('preview')):
    target_page = _normalize_page_target(page)
    if target_page in {'problems', 'contests'}:
        target_page = 'general'
    elif target_page not in {'general', 'preview'}:
        target_page = 'preview'
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    safe_problem_name = str(ctx['problem'].get('name') or '').strip()
    with config.workspace_service.workspace_lock(workspace):
        content_path = _safe_workspace_path(workspace, STATEMENT_CONTENT_REL.as_posix())
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(str(content_tex), encoding='utf-8')
        render_statement_main(workspace / 'statement', problem_title=safe_problem_name)
    _audit(ctx['user']['id'], ctx['problem']['id'], 'preview.save_sources', {'content_bytes': len(str(content_tex).encode('utf-8')), 'problem_name': safe_problem_name})
    return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message='statement saved')

def verification_start(problem: str, user: str, page: str=Form('general')):
    target_page = _normalize_page_target(page)
    if target_page in {'problems', 'contests'}:
        target_page = 'general'
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    invocation_id = _allocate_invocation_id()
    verification_details: dict[str, object] = {'status': 'running', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'invocation_id': invocation_id, 'run_id': '', 'run_ids': [], 'run_count': 0, 'invocation_backend': config.invocation_backend_service.active_backend_name(), 'error': ''}
    msg = 'verification running'
    try:
        solution_options, accepted_source, _ = _run_solution_options_context(workspace)
        accepted_source = str(accepted_source or '').strip()
        if not accepted_source:
            raise ValueError('main correct solution is required')
        if not _workspace_rel_file_exists(workspace, accepted_source):
            raise ValueError('main correct solution source does not exist')
        targets: list[dict[str, str]] = []
        for row in solution_options:
            source_path = str(row.get('path') or '').strip()
            if not source_path:
                continue
            expected_behavior = normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))
            if source_path == accepted_source:
                expected_behavior = 'accepted'
            if expected_behavior == 'unknown' and bool(row.get('is_accepted')):
                expected_behavior = 'accepted'
            targets.append({'path': source_path, 'expected_behavior': expected_behavior})
        if not targets:
            raise ValueError('at least one solution source is required')
        if not any((str(item.get('expected_behavior') or '') == 'accepted' for item in targets)):
            raise ValueError('accepted solution source is required')
        targets.sort(key=lambda item: (0 if item.get('expected_behavior') == 'accepted' else 1, str(item.get('path') or '')))
        planned_run_ids: list[str] = []
        for target in targets:
            run_token = _allocate_run_id()
            target['run_id'] = run_token
            planned_run_ids.append(run_token)
        verification_details['submission_paths'] = [str(item.get('path') or '') for item in targets]
        verification_details['solution_count'] = len(targets)
        verification_details['run_ids'] = planned_run_ids
        verification_details['run_count'] = len(planned_run_ids)
        verification_details['run_id'] = planned_run_ids[0] if planned_run_ids else ''
        started = _start_verification_job(
            problem,
            user,
            actor_user_id=int(ctx['user']['id']),
            problem_id=int(ctx['problem']['id']),
            workspace_id=int(ctx['workspace']['id']),
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=targets,
            invocation_id=invocation_id,
            initial_details=verification_details,
            workspace_path=workspace,
        )
        msg = 'verification running' if started else 'verification already running'
    except Exception as exc:
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
        msg = f'verification failed: {exc}'
    base = f'/problems/{problem}/{user}/{target_page}'
    if str(verification_details.get('status') or '') == 'failed':
        _audit(ctx['user']['id'], ctx['problem']['id'], 'verification.start', verification_details)
        _cleanup_runtime_cache(force=False)
    return _redirect_response(base, status_code=303, message=msg)
