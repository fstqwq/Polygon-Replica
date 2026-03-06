from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from app.impl.auth import (
    _redirect_response,
    _template_response,
)
from app.impl.config import config
from app.main_utils import (
    _normalize_workspace_rel_path,
    _safe_workspace_path,
    _sanitize_log_text_for_ui,
)
from app.services.solution_metadata import normalize_expected_behavior
from app.services.statement_template import (
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    statement_editor_content_rel,
    statement_sources_signature,
)
from app.services.tests_spec import (
    TESTS_SPEC_MANUAL_MAX_CHARS,
    TESTS_SPEC_REL,
    next_test_id,
    normalize_gen_command,
    normalize_manual_input,
    normalize_sample_input,
    normalize_sample_output,
    normalize_test_id,
    normalize_test_kind,
    normalize_tests_spec_entry,
)

from app.impl.workspace import (
    _allocate_invocation_id,
    _allocate_run_id,
    _artifact_root,
    _audit,
    _normalize_page_target,
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

_STATEMENT_INTERACTION_DEFAULT = "Describe the interaction protocol.\n"
_STATEMENT_ATTACHMENT_IMAGE_EXTENSIONS = {
    '.bmp',
    '.gif',
    '.jpeg',
    '.jpg',
    '.png',
    '.svg',
    '.tif',
    '.tiff',
    '.webp',
}


def _is_statement_attachment_image_path(rel_path: str) -> bool:
    return Path(str(rel_path or '')).suffix.lower() in _STATEMENT_ATTACHMENT_IMAGE_EXTENSIONS


def _statement_attachment_rows(workspace: Path, section_dir_rel: str) -> list[dict[str, str]]:
    safe_section_dir = _normalize_workspace_rel_path(section_dir_rel)
    if not safe_section_dir:
        return []
    try:
        section_dir_abs = _safe_workspace_path(workspace, safe_section_dir)
    except HTTPException:
        return []
    if not section_dir_abs.exists() or (not section_dir_abs.is_dir()) or section_dir_abs.is_symlink():
        return []
    workspace_root = workspace.resolve()
    rows: list[dict[str, str]] = []
    try:
        for item in sorted(section_dir_abs.rglob('*')):
            if not item.is_file() or item.is_symlink():
                continue
            try:
                rel = item.resolve().relative_to(workspace_root).as_posix()
            except (ValueError, OSError):
                continue
            if not _is_statement_attachment_image_path(rel):
                continue
            rows.append({'path': rel, 'path_q': quote_plus(rel)})
    except OSError:
        return rows
    return rows


def _tests_spec_gen_script_context(workspace: Path) -> dict[str, object]:
    lines: list[str] = []
    with config.workspace_service.workspace_lock(workspace):
        entries, _spec_path = _read_tests_spec(workspace)
        for entry in entries:
            kind = str(entry.get('kind') or '').strip().lower()
            if kind != 'gen':
                continue
            command = str(_tests_spec_read_payload(workspace, entry) or '').replace('\r\n', '\n').replace('\r', '\n').strip()
            if not command:
                continue
            lines.append(command)
    return {'text': '\n'.join(lines), 'count': len(lines)}


def _parse_gen_script_lines(raw: object) -> list[str]:
    normalized = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    commands: list[str] = []
    for line in normalized.split('\n'):
        cmd = str(line or '').strip()
        if not cmd:
            continue
        commands.append(normalize_gen_command(cmd))
    return commands


def _tests_spec_sample_input_value(raw: object | None, fallback: object = '') -> str:
    if raw is None:
        return normalize_sample_input(fallback)
    return normalize_sample_input(_tests_spec_form_text(raw))


def _tests_spec_sample_output_value(raw: object | None, fallback: object = '') -> str:
    if raw is None:
        return normalize_sample_output(fallback)
    return normalize_sample_output(_tests_spec_form_text(raw))


def _tests_spec_sample_output_validate_value(raw: object | None, fallback: object = True) -> bool:
    if raw is None:
        return _tests_spec_bool_flag(fallback)
    return _tests_spec_bool_flag(_tests_spec_form_text(raw))


def _tests_spec_row(
    *,
    test_id: str,
    kind: str,
    sample: bool,
    sample_input: str = '',
    sample_output: str = '',
    sample_output_validate: bool = True,
    index: int = 0,
) -> dict:
    payload: dict[str, object] = {
        'id': normalize_test_id(test_id),
        'kind': normalize_test_kind(kind),
        'sample': bool(sample),
    }
    safe_sample_input = normalize_sample_input(sample_input)
    safe_sample_output = normalize_sample_output(sample_output)
    if safe_sample_input:
        payload['sample_input'] = safe_sample_input
    if safe_sample_output:
        payload['sample_output'] = safe_sample_output
    if not bool(sample_output_validate):
        payload['sample_output_validate'] = False
    return normalize_tests_spec_entry(payload, index=index)


def _tests_spec_add_single_entry(
    workspace: Path,
    *,
    requested_id: str,
    kind: str,
    sample: bool,
    payload: str,
    sample_input: str,
    sample_output: str,
    sample_output_validate: bool,
) -> tuple[int, str]:
    entries, spec_path = _read_tests_spec(workspace)
    safe_test_id = normalize_test_id(requested_id) if requested_id else next_test_id(entries)
    if any((str(row.get('id') or '').strip() == safe_test_id for row in entries)):
        raise ValueError(f'test id already exists: {safe_test_id}')
    entries.append(
        _tests_spec_row(
            test_id=safe_test_id,
            kind=kind,
            sample=sample,
            sample_input=sample_input,
            sample_output=sample_output,
            sample_output_validate=sample_output_validate,
            index=len(entries) + 1,
        )
    )
    _write_tests_spec(spec_path, entries)
    _tests_spec_write_payload(workspace, safe_test_id, kind, payload)
    return len(entries), safe_test_id

def build_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    tests_editor_error = ''
    tests_gen_script = {'text': '', 'count': 0}
    try:
        tests_editor = _tests_spec_editor_context(workspace)
    except (ValueError, OSError) as exc:
        tests_editor_error = str(exc)
        tests_editor = {'path': TESTS_SPEC_REL.as_posix(), 'exists': False, 'entries': [], 'rows': [], 'summary': {'total': 0, 'manual': 0, 'gen': 0}, 'total': 0, 'shown': 0, 'truncated': False}
    try:
        tests_gen_script = _tests_spec_gen_script_context(workspace)
    except (ValueError, OSError):
        tests_gen_script = {'text': '', 'count': 0}
    if tests_editor_error:
        return _template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor, 'tests_gen_script': tests_gen_script, 'message': tests_editor_error})
    return _template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor, 'tests_gen_script': tests_gen_script})

def tests_spec_add_manual(
    problem: str,
    user: str,
    test_id: str=Form(''),
    sample: str=Form('0'),
    manual_input: str=Form(''),
    sample_input: str | None = Form(None),
    sample_output: str | None = Form(None),
    sample_output_validate: str | None = Form(None),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'manual test added'
    redirect_query = ''
    try:
        safe_input = normalize_manual_input(_tests_spec_form_text(manual_input))
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        requested_id = _tests_spec_form_text(test_id).strip()
        safe_sample_input = _tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = _tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = _tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = _tests_spec_add_single_entry(
                workspace,
                requested_id=requested_id,
                kind='manual',
                sample=safe_sample,
                payload=safe_input,
                sample_input=safe_sample_input,
                sample_output=safe_sample_output,
                sample_output_validate=safe_sample_output_validate,
            )
            redirect_query = f'focus={added_index}'
        _audit(
            ctx['user']['id'],
            ctx['problem']['id'],
            'tests.spec.add_manual',
            {
                'index': added_index,
                'id': safe_test_id,
                'sample': safe_sample,
                'custom_sample_input': bool(safe_sample_input),
                'custom_sample_output': bool(safe_sample_output),
                'custom_sample_output_validate': bool(safe_sample_output_validate),
            },
        )
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    redirect_url = f'/problems/{problem}/{user}/tests'
    if redirect_query:
        redirect_url = f'{redirect_url}?{redirect_query}'
    return _redirect_response(redirect_url, status_code=303, message=msg)


async def tests_spec_add_manual_upload(
    problem: str,
    user: str,
    test_id: str=Form(''),
    sample: str=Form('0'),
    sample_input: str | None = Form(None),
    sample_output: str | None = Form(None),
    sample_output_validate: str | None = Form(None),
    manual_upload: UploadFile=File(...),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'manual test added'
    redirect_query = ''
    try:
        max_raw_bytes = max(4096, int(TESTS_SPEC_MANUAL_MAX_CHARS) * 4)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await manual_upload.read(1024 * 1024)
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
        safe_input = normalize_manual_input(uploaded_text)
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        requested_id = _tests_spec_form_text(test_id).strip()
        safe_sample_input = _tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = _tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = _tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = _tests_spec_add_single_entry(
                workspace,
                requested_id=requested_id,
                kind='manual',
                sample=safe_sample,
                payload=safe_input,
                sample_input=safe_sample_input,
                sample_output=safe_sample_output,
                sample_output_validate=safe_sample_output_validate,
            )
            redirect_query = f'focus={added_index}'
        _audit(
            ctx['user']['id'],
            ctx['problem']['id'],
            'tests.spec.add_manual_upload',
            {
                'index': added_index,
                'id': safe_test_id,
                'sample': safe_sample,
                'bytes': len(safe_input.encode('utf-8', errors='replace')),
                'custom_sample_input': bool(safe_sample_input),
                'custom_sample_output': bool(safe_sample_output),
                'custom_sample_output_validate': bool(safe_sample_output_validate),
            },
        )
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    finally:
        try:
            await manual_upload.close()
        except Exception:
            pass
    redirect_url = f'/problems/{problem}/{user}/tests'
    if redirect_query:
        redirect_url = f'{redirect_url}?{redirect_query}'
    return _redirect_response(redirect_url, status_code=303, message=msg)

def tests_spec_add_gen(
    problem: str,
    user: str,
    test_id: str=Form(''),
    sample: str=Form('0'),
    command: str=Form(''),
    sample_input: str | None = Form(None),
    sample_output: str | None = Form(None),
    sample_output_validate: str | None = Form(None),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator test added'
    redirect_query = ''
    try:
        safe_command = normalize_gen_command(_tests_spec_form_text(command))
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        requested_id = _tests_spec_form_text(test_id).strip()
        safe_sample_input = _tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = _tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = _tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = _tests_spec_add_single_entry(
                workspace,
                requested_id=requested_id,
                kind='gen',
                sample=safe_sample,
                payload=safe_command,
                sample_input=safe_sample_input,
                sample_output=safe_sample_output,
                sample_output_validate=safe_sample_output_validate,
            )
            redirect_query = f'focus={added_index}'
        _audit(
            ctx['user']['id'],
            ctx['problem']['id'],
            'tests.spec.add_gen',
            {
                'index': added_index,
                'id': safe_test_id,
                'sample': safe_sample,
                'command': safe_command,
                'custom_sample_input': bool(safe_sample_input),
                'custom_sample_output': bool(safe_sample_output),
                'custom_sample_output_validate': bool(safe_sample_output_validate),
            },
        )
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    redirect_url = f'/problems/{problem}/{user}/tests'
    if redirect_query:
        redirect_url = f'{redirect_url}?{redirect_query}'
    return _redirect_response(redirect_url, status_code=303, message=msg)

def tests_spec_edit(
    problem: str,
    user: str,
    index: str=Form(...),
    test_id: str=Form(''),
    kind: str=Form(''),
    sample: str=Form('0'),
    payload: str=Form(''),
    sample_input: str | None = Form(None),
    sample_output: str | None = Form(None),
    sample_output_validate: str | None = Form(None),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test updated'
    try:
        safe_test_id = normalize_test_id(_tests_spec_form_text(test_id))
        safe_kind = normalize_test_kind(_tests_spec_form_text(kind))
        safe_sample = _tests_spec_bool_flag(_tests_spec_form_text(sample))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            idx = _tests_spec_resolve_index(index, len(entries))
            current = normalize_tests_spec_entry(dict(entries[idx - 1]), index=idx)
            old_id = normalize_test_id(current.get('id'))
            if safe_test_id != old_id and any((str(row.get('id') or '').strip() == safe_test_id for i, row in enumerate(entries) if i != idx - 1)):
                raise ValueError(f'test id already exists: {safe_test_id}')
            safe_sample_input = _tests_spec_sample_input_value(sample_input, current.get('sample_input', ''))
            safe_sample_output = _tests_spec_sample_output_value(sample_output, current.get('sample_output', ''))
            safe_sample_output_validate = _tests_spec_sample_output_validate_value(
                sample_output_validate,
                current.get('sample_output_validate', True),
            )
            submitted_payload = _tests_spec_form_text(payload)
            if not str(submitted_payload):
                submitted_payload = _tests_spec_read_payload(workspace, current)
            if safe_kind == 'manual':
                safe_payload = normalize_manual_input(submitted_payload)
            elif safe_kind == 'gen':
                safe_payload = normalize_gen_command(submitted_payload)
            else:
                raise ValueError('invalid test kind')
            entries[idx - 1] = _tests_spec_row(
                test_id=safe_test_id,
                kind=safe_kind,
                sample=safe_sample,
                sample_input=safe_sample_input,
                sample_output=safe_sample_output,
                sample_output_validate=safe_sample_output_validate,
                index=idx,
            )
            _write_tests_spec(spec_path, entries)
            _tests_spec_write_payload(workspace, safe_test_id, safe_kind, safe_payload)
            if safe_test_id != old_id:
                _tests_spec_remove_payload(workspace, old_id)
        _audit(
            ctx['user']['id'],
            ctx['problem']['id'],
            'tests.spec.edit',
            {
                'index': idx,
                'id': safe_test_id,
                'kind': safe_kind,
                'sample': safe_sample,
                'custom_sample_input': bool(safe_sample_input),
                'custom_sample_output': bool(safe_sample_output),
                'custom_sample_output_validate': bool(safe_sample_output_validate),
            },
        )
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

def tests_spec_reindex(problem: str, user: str, test_id: str=Form(''), source_index: str=Form(''), target_index: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test reindexed'
    redirect_query = ''
    try:
        safe_test_id_raw = _tests_spec_form_text(test_id).strip()
        safe_source_index_raw = _tests_spec_form_text(source_index).strip()
        target_raw = _tests_spec_form_text(target_index).strip()
        try:
            target_pos = int(target_raw)
        except Exception as exc:
            raise ValueError('target position must be an integer') from exc
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            if not entries:
                raise ValueError('no tests to reindex')
            if target_pos < 1 or target_pos > len(entries):
                raise ValueError('target position is out of range')
            source_pos = -1
            if safe_source_index_raw:
                try:
                    source_pos = _tests_spec_resolve_index(safe_source_index_raw, len(entries)) - 1
                except (ValueError, HTTPException) as exc:
                    raise ValueError('source position is invalid') from exc
            else:
                safe_test_id = normalize_test_id(safe_test_id_raw)
                for i, row in enumerate(entries):
                    if str(row.get('id') or '').strip() == safe_test_id:
                        source_pos = i
                        break
                if source_pos < 0:
                    raise ValueError(f'test id not found: {safe_test_id}')
            row = entries.pop(source_pos)
            entries.insert(target_pos - 1, row)
            _write_tests_spec(spec_path, entries)
            redirect_query = f'focus={target_pos}'
        audit_payload = {'target': target_pos, 'source_index': source_pos + 1}
        if safe_test_id_raw:
            audit_payload['id'] = safe_test_id_raw
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.reindex', audit_payload)
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    url = f'/problems/{problem}/{user}/tests'
    if redirect_query:
        url = f'{url}?{redirect_query}'
    return _redirect_response(url, status_code=303, message=msg)


def tests_spec_gen_script_save(problem: str, user: str, gen_script_text: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'gen script updated'
    try:
        desired_commands = _parse_gen_script_lines(_tests_spec_form_text(gen_script_text))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = _read_tests_spec(workspace)
            existing_gen_rows: list[dict[str, object]] = []
            seed_entries: list[dict[str, object]] = []
            for idx, row in enumerate(entries, start=1):
                normalized_row = normalize_tests_spec_entry(row, index=idx)
                row_id = normalize_test_id(normalized_row.get('id'))
                seed_entries.append({'id': row_id})
                if str(normalized_row.get('kind') or '').strip().lower() == 'gen':
                    existing_gen_rows.append(
                        {
                            'id': row_id,
                            'sample': bool(normalized_row.get('sample')),
                            'sample_input': str(normalized_row.get('sample_input') or ''),
                            'sample_output': str(normalized_row.get('sample_output') or ''),
                            'sample_output_validate': bool(normalized_row.get('sample_output_validate', True)),
                        }
                    )
            replacement_gen_rows: list[dict[str, object]] = []
            for idx, command in enumerate(desired_commands):
                if idx < len(existing_gen_rows):
                    safe_test_id = str(existing_gen_rows[idx].get('id') or '').strip()
                    safe_sample = bool(existing_gen_rows[idx].get('sample'))
                    safe_sample_input = str(existing_gen_rows[idx].get('sample_input') or '')
                    safe_sample_output = str(existing_gen_rows[idx].get('sample_output') or '')
                    safe_sample_output_validate = bool(existing_gen_rows[idx].get('sample_output_validate', True))
                else:
                    safe_test_id = next_test_id(seed_entries)
                    seed_entries.append({'id': safe_test_id})
                    safe_sample = False
                    safe_sample_input = ''
                    safe_sample_output = ''
                    safe_sample_output_validate = True
                replacement_gen_rows.append(
                    {
                        'id': safe_test_id,
                        'kind': 'gen',
                        'sample': safe_sample,
                        'sample_input': safe_sample_input,
                        'sample_output': safe_sample_output,
                        'sample_output_validate': safe_sample_output_validate,
                        'command': command,
                    }
                )
            rebuilt_entries: list[dict[str, object]] = []
            replacement_idx = 0
            for idx, row in enumerate(entries, start=1):
                normalized_row = normalize_tests_spec_entry(row, index=idx)
                kind = str(normalized_row.get('kind') or '').strip().lower()
                if kind == 'gen':
                    if replacement_idx >= len(replacement_gen_rows):
                        continue
                    replacement = replacement_gen_rows[replacement_idx]
                    replacement_idx += 1
                    rebuilt_entries.append(
                        _tests_spec_row(
                            test_id=str(replacement['id']),
                            kind='gen',
                            sample=bool(replacement['sample']),
                            sample_input=str(replacement.get('sample_input') or ''),
                            sample_output=str(replacement.get('sample_output') or ''),
                            sample_output_validate=bool(replacement.get('sample_output_validate', True)),
                            index=len(rebuilt_entries) + 1,
                        )
                    )
                    continue
                rebuilt_entries.append(
                    _tests_spec_row(
                        test_id=normalize_test_id(normalized_row.get('id')),
                        kind=normalize_test_kind(normalized_row.get('kind')),
                        sample=bool(normalized_row.get('sample')),
                        sample_input=str(normalized_row.get('sample_input') or ''),
                        sample_output=str(normalized_row.get('sample_output') or ''),
                        sample_output_validate=bool(normalized_row.get('sample_output_validate', True)),
                        index=len(rebuilt_entries) + 1,
                    )
                )
            for replacement in replacement_gen_rows[replacement_idx:]:
                rebuilt_entries.append(
                    _tests_spec_row(
                        test_id=str(replacement['id']),
                        kind='gen',
                        sample=bool(replacement['sample']),
                        sample_input=str(replacement.get('sample_input') or ''),
                        sample_output=str(replacement.get('sample_output') or ''),
                        sample_output_validate=bool(replacement.get('sample_output_validate', True)),
                        index=len(rebuilt_entries) + 1,
                    )
                )
            _write_tests_spec(spec_path, rebuilt_entries)
            old_gen_ids = {str(row.get('id') or '').strip() for row in existing_gen_rows if str(row.get('id') or '').strip()}
            new_gen_ids = {str(row.get('id') or '').strip() for row in replacement_gen_rows if str(row.get('id') or '').strip()}
            for replacement in replacement_gen_rows:
                safe_test_id = str(replacement.get('id') or '').strip()
                safe_command = str(replacement.get('command') or '')
                _tests_spec_write_payload(workspace, safe_test_id, 'gen', safe_command)
            for removed_id in sorted(old_gen_ids - new_gen_ids):
                _tests_spec_remove_payload(workspace, removed_id)
        _audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.gen_script', {'commands': len(desired_commands)})
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

def _statement_mode_from_ctx(ctx: dict) -> str:
    raw_mode = str(((ctx.get('general_cfg') or {}).get('mode')) or '').strip().lower()
    allowed = {str(item).strip().lower() for item in _C.GENERAL_MODE_VALUES}
    if raw_mode in allowed:
        return raw_mode
    return str(_C.GENERAL_CONFIG_DEFAULTS.get('mode') or 'pass-fail').strip().lower()


def _statement_editor_section_paths(workspace: Path) -> dict[str, Path]:
    legend_rel = statement_editor_content_rel(workspace)
    section_root = legend_rel.parent
    return {
        'legend': section_root / 'legend.tex',
        'input': section_root / 'input.tex',
        'output': section_root / 'output.tex',
        'interaction': section_root / 'interaction.tex',
        'notes': section_root / 'notes.tex',
    }


def _normalize_statement_target_page(page: str) -> str:
    target_page = _normalize_page_target(page)
    if target_page in {'problems', 'contests'}:
        return 'statement'
    if target_page not in {'statement', 'preview'}:
        return 'preview'
    return target_page


def _normalize_verification_target_page(page: str) -> str:
    target_page = _normalize_page_target(page)
    if target_page in {'problems', 'contests', 'settings'}:
        return 'statement'
    if target_page == 'git':
        return 'workspace'
    return target_page


def _statement_editor_sections(workspace: Path, mode: str) -> tuple[list[dict[str, object]], dict[str, str], bool]:
    section_paths = _statement_editor_section_paths(workspace)
    interaction_enabled = str(mode or '').strip().lower() != 'pass-fail'
    specs: tuple[tuple[str, str, str, str], ...] = (
        ('legend', 'legend_tex', 'Legend', ''),
        ('input', 'input_tex', 'Input', ''),
        ('output', 'output_tex', 'Output', ''),
        ('interaction', 'interaction_tex', 'Interaction Protocol', _STATEMENT_INTERACTION_DEFAULT),
        ('notes', 'notes_tex', 'Notes', ''),
    )
    rows: list[dict[str, object]] = []
    path_map: dict[str, str] = {}
    for key, field_name, label, fallback in specs:
        rel = section_paths[key]
        content_text, content_truncated = _read_workspace_source_with_default(workspace, rel, fallback)
        enabled = key != 'interaction' or interaction_enabled
        rows.append(
            {
                'key': key,
                'label': label,
                'field_name': field_name,
                'path': rel.as_posix(),
                'content': content_text,
                'truncated': bool(content_truncated),
                'enabled': bool(enabled),
            }
        )
        path_map[key] = rel.as_posix()
    return rows, path_map, interaction_enabled


def _extract_latex_failure_summary(log_text: str, summary_obj: dict[str, object] | None=None) -> str:
    text = str(log_text or '')
    lines = text.splitlines()
    if lines:
        file_hint = ''
        star_file_re = re.compile('^\\*\\*(?P<file>[^\\s]+\\.tex)\\s*$')
        open_file_re = re.compile('\\((?:\\./)?(?P<file>[^()\\s]+\\.tex)\\b')
        line_re = re.compile('^l\\.(?P<line>\\d+)\\s*(?P<context>.*)$')
        for raw in lines:
            stripped = str(raw or '').strip()
            if not stripped:
                continue
            m_star = star_file_re.match(stripped)
            if m_star:
                file_hint = str(m_star.group('file') or '').strip()
                break
            m_open = open_file_re.search(stripped)
            if m_open:
                file_hint = str(m_open.group('file') or '').strip()
                break
        for idx, raw in enumerate(lines):
            stripped = str(raw or '').strip()
            if not stripped.startswith('!'):
                continue
            error_msg = stripped[1:].strip()
            if not error_msg:
                continue
            line_no = ''
            for j in range(idx + 1, min(len(lines), idx + 8)):
                probe = str(lines[j] or '').strip()
                m_line = line_re.match(probe)
                if not m_line:
                    continue
                line_no = str(m_line.group('line') or '').strip()
                break
            if file_hint and line_no:
                return f'{file_hint}:{line_no} {error_msg}'
            if line_no:
                return f'line {line_no}: {error_msg}'
            return error_msg
        noise_prefixes = (
            'this is pdftex',
            'entering extended mode',
            'restricted /write18 enabled',
            '%&-line parsing enabled',
            '**',
        )
        for raw in lines:
            stripped = str(raw or '').strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if any((lowered.startswith(prefix) for prefix in noise_prefixes)):
                continue
            return stripped
    if isinstance(summary_obj, dict):
        return str(summary_obj.get('error') or '').strip()
    return ''


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
            cached_id = config.preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                source_commit=head_commit,
                statement_signature=current_statement_signature,
                allow_cache_mutation=False,
            )
            if cached_id:
                preview_id = cached_id
        elif dirty:
            cached_id = config.preview_service.find_cached_preview_id(
                problem,
                problem_id,
                workspace_id,
                source_commit=None,
                statement_signature=current_statement_signature,
                allow_cache_mutation=False,
            )
            if cached_id:
                preview_id = cached_id
    if preview_id and (not requested_preview_id) and (not _preview_has_visible_output(preview_id)):
        preview_id = ''
    safe_mode = _statement_mode_from_ctx(ctx)
    statement_sections, section_path_map, interaction_section_enabled = _statement_editor_sections(workspace, safe_mode)
    log = ''
    log_truncated = False
    pdf_exists = False
    log_refs = []
    log_refs_total = 0
    log_refs_truncated = False
    selected_preview_nav: dict[str, object] | None = None
    selected_preview_summary: dict[str, object] | None = None
    selected_preview_status = 'none'
    preview_compile_failed = False
    compile_error_summary = ''
    latex_log_href = ''

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
        preview_row = config.db.fetch_one('SELECT id,status,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [preview_id, problem_id, workspace_id])
        if preview_row is None:
            preview_id = ''
        else:
            selected_preview_status = str(preview_row['status'] or 'none').strip().lower() or 'none'
            summary_obj = _parse_summary_json(preview_row['summary_json'], f'preview/{preview_id}') or {}
            if isinstance(summary_obj, dict):
                selected_preview_summary = summary_obj
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
            latex_log_href = f'/problems/{problem}/{user}/artifacts/{preview_id}/logs/latex.log'
            raw_log, log_truncated = _read_text_safe_limited(lp, _C.UI_LOG_TEXT_CHAR_LIMIT)
            redact_prefixes: list[tuple[str, str]] = [(str(workspace.resolve()), '.'), (str(config.settings.workspace_root.resolve()), '__workspace_root__'), (str(config.settings.artifacts_root.resolve()), '__artifacts__'), (str(config.settings.run_root.resolve()), '__runs__'), (str(config.settings.cache_root.resolve()), '__cache__')]
            log = _sanitize_log_text_for_ui(raw_log, path_prefixes=redact_prefixes)
            if not str(log or '').strip():
                log = '(empty)'
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
        preview_compile_failed = selected_preview_status in {'failed', 'error'}
        if preview_compile_failed:
            if log_refs:
                compile_error_summary = str(log_refs[0].get('context') or '').strip()
            if not compile_error_summary:
                compile_error_summary = _extract_latex_failure_summary(log, selected_preview_summary)
            if len(compile_error_summary) > 240:
                compile_error_summary = compile_error_summary[:237].rstrip() + '...'
    if selected_preview_nav is not None and isinstance(ctx.get('nav_status'), dict):
        ctx['nav_status']['preview'] = selected_preview_nav
    request_path = str(getattr(request.url, 'path', '') or '')
    return_page = 'preview' if request_path.endswith('/preview') else 'statement'
    statement_section_dir = Path(section_path_map.get('legend') or 'statement-sections/english/legend.tex').parent.as_posix()
    statement_attachments = _statement_attachment_rows(workspace, statement_section_dir)
    return _template_response(
        request,
        'preview.html',
        {
            'ctx': ctx,
            'message': message,
            'preview_id': preview_id,
            'previews': previews,
            'statement_sections': statement_sections,
            'statement_section_paths': section_path_map,
            'statement_section_dir': statement_section_dir,
            'interaction_section_enabled': bool(interaction_section_enabled),
            'statement_template_path': STATEMENT_TEMPLATE_REL.as_posix(),
            'statement_problem_path': STATEMENT_PROBLEM_REL.as_posix(),
            'statement_style_path': STATEMENT_STYLE_REL.as_posix(),
            'statement_attachments': statement_attachments,
            'editor_char_limit': _C.STATEMENT_EDITOR_CHAR_LIMIT,
            'log': log,
            'log_truncated': log_truncated,
            'log_char_limit': _C.UI_LOG_TEXT_CHAR_LIMIT,
            'pdf_exists': pdf_exists,
            'log_refs': log_refs,
            'log_refs_total': log_refs_total,
            'log_refs_truncated': log_refs_truncated,
            'log_refs_limit': _C.PREVIEW_LOG_REF_LIST_LIMIT,
            'preview_compile_failed': preview_compile_failed,
            'compile_error_summary': compile_error_summary,
            'latex_log_href': latex_log_href,
            'problem_name_max_len': _C.PROBLEM_NAME_MAX_LEN,
            'problem_mode_values': list(_C.GENERAL_MODE_VALUES),
            'time_limit_min_ms': _C.GENERAL_TIME_LIMIT_MIN_MS,
            'time_limit_max_ms': _C.GENERAL_TIME_LIMIT_MAX_MS,
            'memory_limit_min_mb': _C.GENERAL_MEMORY_LIMIT_MIN_MB,
            'memory_limit_max_mb': _C.GENERAL_MEMORY_LIMIT_MAX_MB,
            'return_page': return_page,
            'statement_mode': safe_mode,
        },
    )

def preview_run(problem: str, user: str, page: str=Form('statement')):
    target_page = _normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    workspace_key = f'{problem_id}:{workspace_id}'
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
    msg = 'preview compile failed'
    base = f'/problems/{problem}/{user}/{target_page}'
    with config.preview_lock:
        if workspace_key in config.preview_inflight:
            details['status'] = 'running'
            details['preview_status'] = 'running'
            details['error'] = 'preview compile already running'
            _audit(ctx['user']['id'], problem_id, 'preview.run', details)
            return _redirect_response(base, status_code=303, message='preview compile already running')
        config.preview_inflight.add(workspace_key)
    try:
        preview_id = config.preview_service.compile_preview(problem, user)
        details['preview_id'] = str(preview_id or '').strip()
        row = config.db.fetch_one(
            'SELECT status,source_commit,source_ref,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?',
            [details['preview_id'], problem_id, workspace_id],
        )
        if row is None:
            raise RuntimeError('preview metadata missing after compile')
        preview_status = str(row['status'] or 'missing').strip().lower()
        details['preview_status'] = preview_status
        details['source_commit'] = str(row['source_commit'] or '').strip()
        details['source_ref'] = str(row['source_ref'] or '').strip()
        summary_obj = _parse_summary_json(row['summary_json'], f"preview/{details['preview_id']}")
        if preview_status == 'ok':
            details['status'] = 'ok'
            msg = 'preview compiled'
        else:
            details['status'] = 'failed'
            details['error'] = str(summary_obj.get('error') or 'preview failed') if isinstance(summary_obj, dict) else 'preview failed'
            msg = 'preview compile failed'
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        msg = str(exc)
    finally:
        with config.preview_lock:
            config.preview_inflight.discard(workspace_key)
        _audit(ctx['user']['id'], problem_id, 'preview.run', details)
    redirect_url = base
    preview_id = str(details.get('preview_id') or '').strip()
    if preview_id:
        redirect_url = f'{base}?preview_id={preview_id}'
    return _redirect_response(redirect_url, status_code=303, message=msg)

def preview_status(problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_key = f'{problem_id}:{workspace_id}'
    with config.preview_lock:
        running = workspace_key in config.preview_inflight
    row = config.db.fetch_one(
        'SELECT id,status,created_at,finished_at FROM previews WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
        [problem_id, workspace_id],
    )
    latest_preview_id = ''
    latest_status = 'missing'
    latest_created_at = ''
    latest_finished_at = ''
    if row is not None:
        latest_preview_id = str(row['id'] or '').strip()
        latest_status = str(row['status'] or 'missing').strip().lower() or 'missing'
        latest_created_at = str(row['created_at'] or '').strip()
        latest_finished_at = str(row['finished_at'] or '').strip()
    return JSONResponse(
        {
            'running': bool(running),
            'latest_preview_id': latest_preview_id,
            'latest_status': latest_status,
            'latest_created_at': latest_created_at,
            'latest_finished_at': latest_finished_at,
        }
    )

def preview_save(
    problem: str,
    user: str,
    legend_tex: str=Form(''),
    input_tex: str=Form(''),
    output_tex: str=Form(''),
    interaction_tex: str=Form(''),
    notes_tex: str=Form(''),
    page: str=Form('statement'),
):
    target_page = _normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    safe_mode = _statement_mode_from_ctx(ctx)
    safe_legend = str(legend_tex or '')
    safe_input = str(input_tex or '')
    safe_output = str(output_tex or '')
    safe_notes = str(notes_tex or '')
    safe_interaction = str(interaction_tex or '')
    with config.workspace_service.workspace_lock(workspace):
        section_paths = _statement_editor_section_paths(workspace)
        write_plan = {
            'legend': safe_legend,
            'input': safe_input,
            'output': safe_output,
            'notes': safe_notes,
        }
        if safe_mode != 'pass-fail':
            write_plan['interaction'] = safe_interaction
        for key, content in write_plan.items():
            rel = section_paths[key]
            section_path = _safe_workspace_path(workspace, rel.as_posix())
            section_path.parent.mkdir(parents=True, exist_ok=True)
            section_path.write_text(content, encoding='utf-8')
    _audit(
        ctx['user']['id'],
        ctx['problem']['id'],
        'preview.save_sources',
        {
            'mode': safe_mode,
            'legend_bytes': len(safe_legend.encode('utf-8')),
            'input_bytes': len(safe_input.encode('utf-8')),
            'output_bytes': len(safe_output.encode('utf-8')),
            'notes_bytes': len(safe_notes.encode('utf-8')),
            'interaction_bytes': len(safe_interaction.encode('utf-8')) if safe_mode != 'pass-fail' else 0,
        },
    )
    return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message='statement saved')


def statement_attachment_delete(problem: str, user: str, path: str=Form(...), page: str=Form('statement')):
    target_page = _normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    _require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    section_dir_rel = _statement_editor_section_paths(workspace)['legend'].parent.as_posix()
    message = 'attachment deleted'
    try:
        safe_rel = _normalize_workspace_rel_path(path)
        if not safe_rel:
            raise ValueError('attachment path is required')
        section_prefix = section_dir_rel.rstrip('/') + '/'
        if safe_rel != section_dir_rel and not safe_rel.startswith(section_prefix):
            raise ValueError('attachment must be under statement section directory')
        if not _is_statement_attachment_image_path(safe_rel):
            raise ValueError('only image attachments are supported')
        with config.workspace_service.workspace_lock(workspace):
            attachment_abs = _safe_workspace_path(workspace, safe_rel)
            if not attachment_abs.exists() or (not attachment_abs.is_file()):
                raise ValueError('attachment not found')
            attachment_abs.unlink()
        _audit(ctx['user']['id'], ctx['problem']['id'], 'statement.attachment.delete', {'path': safe_rel})
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    return _redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=message)

def verification_start(problem: str, user: str, page: str=Form('statement')):
    target_page = _normalize_verification_target_page(page)
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
    return _redirect_response(base, status_code=303, message=msg)
