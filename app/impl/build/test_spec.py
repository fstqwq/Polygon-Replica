from __future__ import annotations

from app.impl.preview.shared import (
    BackgroundTask,
    File,
    FileResponse,
    Form,
    HTTPException,
    Path,
    Request,
    TESTS_SPEC_MANUAL_MAX_CHARS,
    TESTS_SPEC_REL,
    UploadFile,
    audit,
    read_tests_spec,
    redirect_response,
    require_write_access,
    template_response,
    tests_spec_bool_flag,
    tests_spec_editor_context,
    tests_spec_form_text,
    tests_spec_payload_file_path,
    tests_spec_read_payload,
    tests_spec_remove_payload,
    tests_spec_resolve_index,
    tests_spec_write_payload,
    write_tests_spec,
    config,
    next_test_id,
    normalize_gen_command,
    normalize_manual_input,
    normalize_test_id,
    normalize_test_kind,
    normalize_tests_spec_entry,
    os,
    page_ctx,
    tempfile,
    parse_gen_script_lines,
    tests_spec_add_single_entry,
    tests_spec_gen_script_context,
    tests_spec_row,
    tests_spec_sample_input_value,
    tests_spec_sample_output_validate_value,
    tests_spec_sample_output_value,
)
def build_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    tests_editor_error = ''
    tests_gen_script = {'text': '', 'count': 0}
    try:
        tests_editor = tests_spec_editor_context(workspace)
    except (ValueError, OSError) as exc:
        tests_editor_error = str(exc)
        tests_editor = {'path': TESTS_SPEC_REL.as_posix(), 'exists': False, 'entries': [], 'rows': [], 'summary': {'total': 0, 'manual': 0, 'gen': 0}, 'total': 0, 'shown': 0, 'truncated': False}
    try:
        tests_gen_script = tests_spec_gen_script_context(workspace)
    except (ValueError, OSError):
        tests_gen_script = {'text': '', 'count': 0}
    if tests_editor_error:
        return template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor, 'tests_gen_script': tests_gen_script, 'message': tests_editor_error})
    return template_response(request, 'tests.html', {'ctx': ctx, 'tests_editor': tests_editor, 'tests_gen_script': tests_gen_script})

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
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'manual test added'
    redirect_query = ''
    try:
        safe_input = normalize_manual_input(tests_spec_form_text(manual_input))
        safe_sample = tests_spec_bool_flag(tests_spec_form_text(sample))
        requested_id = tests_spec_form_text(test_id).strip()
        safe_sample_input = tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = tests_spec_add_single_entry(
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
        audit(
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
    return redirect_response(redirect_url, status_code=303, message=msg)

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
    require_write_access(ctx)
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
        safe_sample = tests_spec_bool_flag(tests_spec_form_text(sample))
        requested_id = tests_spec_form_text(test_id).strip()
        safe_sample_input = tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = tests_spec_add_single_entry(
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
        audit(
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
    return redirect_response(redirect_url, status_code=303, message=msg)

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
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'generator test added'
    redirect_query = ''
    try:
        safe_command = normalize_gen_command(tests_spec_form_text(command))
        safe_sample = tests_spec_bool_flag(tests_spec_form_text(sample))
        requested_id = tests_spec_form_text(test_id).strip()
        safe_sample_input = tests_spec_sample_input_value(sample_input, '')
        safe_sample_output = tests_spec_sample_output_value(sample_output, '')
        safe_sample_output_validate = tests_spec_sample_output_validate_value(sample_output_validate, True)
        with config.workspace_service.workspace_lock(workspace):
            added_index, safe_test_id = tests_spec_add_single_entry(
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
        audit(
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
    return redirect_response(redirect_url, status_code=303, message=msg)

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
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test updated'
    try:
        safe_test_id = normalize_test_id(tests_spec_form_text(test_id))
        safe_kind = normalize_test_kind(tests_spec_form_text(kind))
        safe_sample = tests_spec_bool_flag(tests_spec_form_text(sample))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = read_tests_spec(workspace)
            idx = tests_spec_resolve_index(index, len(entries))
            current = normalize_tests_spec_entry(dict(entries[idx - 1]), index=idx)
            old_id = normalize_test_id(current.get('id'))
            if safe_test_id != old_id and any((str(row.get('id') or '').strip() == safe_test_id for i, row in enumerate(entries) if i != idx - 1)):
                raise ValueError(f'test id already exists: {safe_test_id}')
            safe_sample_input = tests_spec_sample_input_value(sample_input, current.get('sample_input', ''))
            safe_sample_output = tests_spec_sample_output_value(sample_output, current.get('sample_output', ''))
            safe_sample_output_validate = tests_spec_sample_output_validate_value(
                sample_output_validate,
                current.get('sample_output_validate', True),
            )
            submitted_payload = tests_spec_form_text(payload)
            if not str(submitted_payload):
                submitted_payload = tests_spec_read_payload(workspace, current)
            if safe_kind == 'manual':
                safe_payload = normalize_manual_input(submitted_payload)
            elif safe_kind == 'gen':
                safe_payload = normalize_gen_command(submitted_payload)
            else:
                raise ValueError('invalid test kind')
            entries[idx - 1] = tests_spec_row(
                test_id=safe_test_id,
                kind=safe_kind,
                sample=safe_sample,
                sample_input=safe_sample_input,
                sample_output=safe_sample_output,
                sample_output_validate=safe_sample_output_validate,
                index=idx,
            )
            write_tests_spec(spec_path, entries)
            tests_spec_write_payload(workspace, safe_test_id, safe_kind, safe_payload)
            if safe_test_id != old_id:
                tests_spec_remove_payload(workspace, old_id)
        audit(
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
    return redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_delete(problem: str, user: str, index: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test deleted'
    try:
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = read_tests_spec(workspace)
            idx = tests_spec_resolve_index(index, len(entries))
            deleted = entries.pop(idx - 1)
            deleted_id = str(deleted.get('id') or '').strip()
            write_tests_spec(spec_path, entries)
            if deleted_id:
                tests_spec_remove_payload(workspace, deleted_id)
        audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.delete', {'index': idx, 'kind': str(deleted.get('kind') or ''), 'id': deleted_id})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_reindex(problem: str, user: str, test_id: str=Form(''), source_index: str=Form(''), target_index: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'test reindexed'
    redirect_query = ''
    try:
        safe_test_id_raw = tests_spec_form_text(test_id).strip()
        safe_source_index_raw = tests_spec_form_text(source_index).strip()
        target_raw = tests_spec_form_text(target_index).strip()
        try:
            target_pos = int(target_raw)
        except Exception as exc:
            raise ValueError('target position must be an integer') from exc
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = read_tests_spec(workspace)
            if not entries:
                raise ValueError('no tests to reindex')
            if target_pos < 1 or target_pos > len(entries):
                raise ValueError('target position is out of range')
            source_pos = -1
            if safe_source_index_raw:
                try:
                    source_pos = tests_spec_resolve_index(safe_source_index_raw, len(entries)) - 1
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
            write_tests_spec(spec_path, entries)
            redirect_query = f'focus={target_pos}'
        audit_payload = {'target': target_pos, 'source_index': source_pos + 1}
        if safe_test_id_raw:
            audit_payload['id'] = safe_test_id_raw
        audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.reindex', audit_payload)
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    url = f'/problems/{problem}/{user}/tests'
    if redirect_query:
        url = f'{url}?{redirect_query}'
    return redirect_response(url, status_code=303, message=msg)

def tests_spec_gen_script_save(problem: str, user: str, gen_script_text: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'gen script updated'
    try:
        desired_commands = parse_gen_script_lines(tests_spec_form_text(gen_script_text))
        with config.workspace_service.workspace_lock(workspace):
            entries, spec_path = read_tests_spec(workspace)
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
                        tests_spec_row(
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
                    tests_spec_row(
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
                    tests_spec_row(
                        test_id=str(replacement['id']),
                        kind='gen',
                        sample=bool(replacement['sample']),
                        sample_input=str(replacement.get('sample_input') or ''),
                        sample_output=str(replacement.get('sample_output') or ''),
                        sample_output_validate=bool(replacement.get('sample_output_validate', True)),
                        index=len(rebuilt_entries) + 1,
                    )
                )
            write_tests_spec(spec_path, rebuilt_entries)
            old_gen_ids = {str(row.get('id') or '').strip() for row in existing_gen_rows if str(row.get('id') or '').strip()}
            new_gen_ids = {str(row.get('id') or '').strip() for row in replacement_gen_rows if str(row.get('id') or '').strip()}
            for replacement in replacement_gen_rows:
                safe_test_id = str(replacement.get('id') or '').strip()
                safe_command = str(replacement.get('command') or '')
                tests_spec_write_payload(workspace, safe_test_id, 'gen', safe_command)
            for removed_id in sorted(old_gen_ids - new_gen_ids):
                tests_spec_remove_payload(workspace, removed_id)
        audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.gen_script', {'commands': len(desired_commands)})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)

def tests_spec_payload_download(problem: str, user: str, index: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    with config.workspace_service.workspace_lock(workspace):
        entries, _spec_path = read_tests_spec(workspace)
        idx = tests_spec_resolve_index(index, len(entries))
        entry = dict(entries[idx - 1])
        test_id = normalize_test_id(entry.get('id'))
        kind = normalize_test_kind(entry.get('kind'))
        payload_path: Path | None = None
        try:
            payload_path = tests_spec_payload_file_path(workspace, test_id, kind)
        except (HTTPException, ValueError):
            payload_path = None
        if payload_path is not None:
            try:
                if payload_path.exists() and payload_path.is_file() and (not payload_path.is_symlink()):
                    return FileResponse(payload_path, filename=f'{test_id}.in', media_type='text/plain; charset=utf-8')
            except OSError:
                pass
        payload_text = tests_spec_read_payload(workspace, entry)
    fd, tmp_name = tempfile.mkstemp(prefix=f'test-{test_id}-', suffix='.in')
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.write_text(payload_text, encoding='utf-8')
    return FileResponse(tmp_path, filename=f'{test_id}.in', media_type='text/plain; charset=utf-8', background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)))

async def tests_spec_payload_upload(problem: str, user: str, index: str=Form(...), payload_upload: UploadFile=File(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
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
            entries, _spec_path = read_tests_spec(workspace)
            idx = tests_spec_resolve_index(index, len(entries))
            entry = dict(entries[idx - 1])
            test_id = normalize_test_id(entry.get('id'))
            kind = normalize_test_kind(entry.get('kind'))
            if kind != 'manual':
                raise ValueError('payload upload is only available for manual tests')
            tests_spec_write_payload(workspace, test_id, 'manual', safe_payload)
        audit(ctx['user']['id'], ctx['problem']['id'], 'tests.spec.payload.upload', {'index': idx, 'id': test_id, 'bytes': len(safe_payload.encode('utf-8', errors='replace'))})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    finally:
        try:
            await payload_upload.close()
        except Exception:
            pass
    return redirect_response(f'/problems/{problem}/{user}/tests', status_code=303, message=msg)


