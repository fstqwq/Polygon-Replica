from __future__ import annotations

from app.impl.preview.shared import (
    Form,
    HTTPException,
    JSONResponse,
    Path,
    Request,
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    _C,
    artifact_root,
    audit,
    normalize_workspace_rel_path,
    parse_summary_json,
    read_text_safe_limited,
    redirect_response,
    require_write_access,
    safe_artifact_path,
    safe_workspace_path,
    sanitize_log_text_for_ui,
    template_response,
    config,
    page_ctx,
    re,
    statement_sources_signature,
    extract_latex_failure_summary,
    is_statement_attachment_image_path,
    normalize_statement_target_page,
    statement_attachment_rows,
    statement_editor_section_paths,
    statement_editor_sections,
    statement_mode_from_ctx,
)
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
            artifact_root(problem, candidate_id)
        except HTTPException:
            return False
        try:
            safe_artifact_path(problem, candidate_id, 'statement_preview/statement.pdf')
            return True
        except HTTPException:
            pass
        try:
            safe_artifact_path(problem, candidate_id, 'logs/latex.log')
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
    safe_mode = statement_mode_from_ctx(ctx)
    statement_sections, section_path_map, interaction_section_enabled = statement_editor_sections(workspace, safe_mode)
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
    preview_failed_stage = ''
    preview_failure_title = 'Compile failed.'
    preview_failure_detail = ''
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
                safe_artifact_path(problem, safe_id, 'statement_preview/statement.pdf')
                has_pdf_output = True
            except HTTPException:
                has_pdf_output = False
            if not has_pdf_output:
                return {'text': 'missing', 'danger': True, 'warn': False}
            summary_obj = parse_summary_json(row['summary_json'], f'preview/{safe_id}') or {}
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
            summary_obj = parse_summary_json(preview_row['summary_json'], f'preview/{preview_id}') or {}
            if isinstance(summary_obj, dict):
                selected_preview_summary = summary_obj
            preview_signature = str(summary_obj.get('statement_signature') or '').strip()
            if (not requested_preview_id) and (preview_signature != current_statement_signature):
                preview_id = ''
    if preview_id:
        try:
            safe_artifact_path(problem, preview_id, 'statement_preview/statement.pdf')
            pdf_exists = True
        except HTTPException:
            pdf_exists = False
        try:
            lp = safe_artifact_path(problem, preview_id, 'logs/latex.log')
        except HTTPException:
            lp = None
        if lp is not None:
            latex_log_href = f'/problems/{problem}/{user}/artifacts/{preview_id}/logs/latex.log'
            raw_log, log_truncated = read_text_safe_limited(lp, _C.UI_LOG_TEXT_CHAR_LIMIT)
            redact_prefixes: list[tuple[str, str]] = [(str(workspace.resolve()), '.'), (str(config.settings.workspace_root.resolve()), '__workspace_root__'), (str(config.settings.artifacts_root.resolve()), '__artifacts__'), (str(config.settings.run_root.resolve()), '__runs__'), (str(config.settings.cache_root.resolve()), '__cache__')]
            log = sanitize_log_text_for_ui(raw_log, path_prefixes=redact_prefixes)
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
            preview_failed_stage = str(selected_preview_summary.get('failed_stage') or '').strip().lower() if isinstance(selected_preview_summary, dict) else ''
            if preview_failed_stage == 'sample_sync':
                preview_failure_title = 'Sample verification failed.'
                preview_failure_detail = sanitize_log_text_for_ui(str((selected_preview_summary or {}).get('error') or ''))
                latex_log_href = ''
                log = ''
                log_truncated = False
                log_refs = []
                log_refs_total = 0
                log_refs_truncated = False
            else:
                if log_refs:
                    preview_failure_detail = str(log_refs[0].get('context') or '').strip()
                if not preview_failure_detail:
                    preview_failure_detail = extract_latex_failure_summary(log, selected_preview_summary)
                if len(preview_failure_detail) > 240:
                    preview_failure_detail = preview_failure_detail[:237].rstrip() + '...'
    if selected_preview_nav is not None and isinstance(ctx.get('nav_status'), dict):
        ctx['nav_status']['preview'] = selected_preview_nav
    request_path = str(getattr(request.url, 'path', '') or '')
    return_page = 'preview' if request_path.endswith('/preview') else 'statement'
    statement_section_dir = Path(section_path_map.get('legend') or 'statement-sections/english/legend.tex').parent.as_posix()
    statement_attachments = statement_attachment_rows(workspace, statement_section_dir)
    return template_response(
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
            'preview_failure_title': preview_failure_title,
            'preview_failure_detail': preview_failure_detail,
            'preview_failed_stage': preview_failed_stage,
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
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    workspace_key = f'{problem_id}:{workspace_id}'
    details: dict[str, object] = {
        'status': 'failed',
        'preview_id': '',
        'preview_status': 'missing',
        'failed_stage': '',
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
            audit(ctx['user']['id'], problem_id, 'preview.run', details)
            return redirect_response(base, status_code=303, message='preview compile already running')
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
        summary_obj = parse_summary_json(row['summary_json'], f"preview/{details['preview_id']}")
        if preview_status == 'ok':
            details['status'] = 'ok'
            msg = 'preview compiled'
        else:
            details['status'] = 'failed'
            details['error'] = str(summary_obj.get('error') or 'preview failed') if isinstance(summary_obj, dict) else 'preview failed'
            failed_stage = str(summary_obj.get('failed_stage') or '').strip().lower() if isinstance(summary_obj, dict) else ''
            details['failed_stage'] = failed_stage
            msg = 'sample verification failed' if failed_stage == 'sample_sync' else 'preview compile failed'
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        msg = str(exc)
    finally:
        with config.preview_lock:
            config.preview_inflight.discard(workspace_key)
        audit(ctx['user']['id'], problem_id, 'preview.run', details)
    redirect_url = base
    preview_id = str(details.get('preview_id') or '').strip()
    if preview_id:
        redirect_url = f'{base}?preview_id={preview_id}'
    return redirect_response(redirect_url, status_code=303, message=msg)

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
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    safe_mode = statement_mode_from_ctx(ctx)
    safe_legend = str(legend_tex or '')
    safe_input = str(input_tex or '')
    safe_output = str(output_tex or '')
    safe_notes = str(notes_tex or '')
    safe_interaction = str(interaction_tex or '')
    with config.workspace_service.workspace_lock(workspace):
        section_paths = statement_editor_section_paths(workspace)
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
            section_path = safe_workspace_path(workspace, rel.as_posix())
            section_path.parent.mkdir(parents=True, exist_ok=True)
            section_path.write_text(content, encoding='utf-8')
    audit(
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
    return redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message='statement saved')

def statement_attachment_delete(problem: str, user: str, path: str=Form(...), page: str=Form('statement')):
    target_page = normalize_statement_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    section_dir_rel = statement_editor_section_paths(workspace)['legend'].parent.as_posix()
    message = 'attachment deleted'
    try:
        safe_rel = normalize_workspace_rel_path(path)
        if not safe_rel:
            raise ValueError('attachment path is required')
        section_prefix = section_dir_rel.rstrip('/') + '/'
        if safe_rel != section_dir_rel and not safe_rel.startswith(section_prefix):
            raise ValueError('attachment must be under statement section directory')
        if not is_statement_attachment_image_path(safe_rel):
            raise ValueError('only image attachments are supported')
        with config.workspace_service.workspace_lock(workspace):
            attachment_abs = safe_workspace_path(workspace, safe_rel)
            if not attachment_abs.exists() or (not attachment_abs.is_file()):
                raise ValueError('attachment not found')
            attachment_abs.unlink()
        audit(ctx['user']['id'], ctx['problem']['id'], 'statement.attachment.delete', {'path': safe_rel})
    except (ValueError, OSError) as exc:
        message = str(exc)
    except HTTPException as exc:
        message = str(exc.detail)
    return redirect_response(f'/problems/{problem}/{user}/{target_page}', status_code=303, message=message)


