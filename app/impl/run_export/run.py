from __future__ import annotations

from app.impl.run_export.context import (
    Path,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    _C,
    allocate_invocation_id,
    allocate_run_id,
    assert_workspace_build_access,
    audit,
    build_run_detail_context,
    dedupe_preserve_order,
    latest_workspace_build,
    normalize_optional_component_source_path,
    normalize_optional_component_source_path_safe,
    normalize_problem_mode,
    normalize_run_id_token,
    normalize_run_test_name_token,
    parse_run_detail_ids,
    parse_run_detail_invocation_id,
    parse_run_test_names,
    read_problem_config,
    record_async_run_failure,
    redirect_response,
    require_write_access,
    run_invocation_scope_run_ids,
    run_list_rows,
    run_solution_options_context,
    run_source_labels_from_audit,
    run_test_options_context,
    start_run_execute_batch,
    template_response,
    config,
    infer_expected_behavior_from_name,
    json,
    normalize_expected_behavior,
    now_iso,
    page_ctx,
    quote_plus,
    upload_compile_check_error,
    workspace_source_compile_check_error,
)
from app.impl.run_export.query import (
    _detail_invocation_id,
    _rerun_solution_paths_from_invocation,
    _run_detail_use_compact_layout,
    _summary_object,
)
def _mark_run_cancelled(run_id: str, reason: str) -> None:
    safe_run_id = normalize_run_id_token(run_id)
    if not safe_run_id:
        return
    row = config.db.fetch_one("SELECT summary_json FROM runs WHERE id=?", [safe_run_id])
    summary = _summary_object(row["summary_json"] if row is not None else None)
    summary["cancelled"] = True
    summary["cancel_reason"] = str(reason or "").strip()
    if not str(summary.get("error") or "").strip():
        summary["error"] = str(reason or "").strip()
    config.db.execute(
        """
        UPDATE runs
        SET status='failed', summary_json=?, finished_at=?
        WHERE id=?
        """,
        [json.dumps(summary), now_iso(), safe_run_id],
    )

def _cancel_judgehost_tasks(run_ids: list[str], reason: str) -> int:
    safe_ids = dedupe_preserve_order([normalize_run_id_token(item) for item in run_ids if normalize_run_id_token(item)])
    service = getattr(config, "judgehost_task_service", None)
    affected = 0
    if not safe_ids:
        return affected
    result_obj = {"error": str(reason or "").strip() or "verification cancelled by user"}
    if service is not None:
        try:
            affected = int(service.cancel_tasks_for_runs(safe_ids, reason=str(result_obj["error"])))
        except Exception:
            affected = 0

    if service is not None:
        try:
            service.cancel_domjudge_jobs_for_runs(safe_ids, final_status="failed")
        except Exception:
            pass

    return affected

def _finalize_cancelled_builds(run_ids: list[str], reason: str) -> int:
    safe_run_ids = dedupe_preserve_order([normalize_run_id_token(item) for item in run_ids if normalize_run_id_token(item)])
    if not safe_run_ids:
        return 0
    placeholders = ",".join(("?" for _ in safe_run_ids))
    build_rows = config.db.fetch_all(
        f"""
        SELECT DISTINCT build_id
        FROM runs
        WHERE id IN ({placeholders})
        """,
        [*safe_run_ids],
    )
    build_ids: list[str] = []
    for row in build_rows:
        if row is None:
            continue
        token = str(row["build_id"] or "").strip()
        if not token:
            continue
        if token == str(_C.RUN_PLACEHOLDER_BUILD_ID):
            continue
        if token not in build_ids:
            build_ids.append(token)
    if not build_ids:
        return 0
    now_text = now_iso()
    cancelled_count = 0
    service = getattr(config, "judgehost_task_service", None)
    cancel_reason = str(reason or "").strip() or "verification cancelled by user"
    for build_id in build_ids:
        active_task_count = 0
        if service is not None:
            try:
                active_task_count = int(service.active_task_count_for_build(build_id))
            except Exception:
                active_task_count = 0
        if active_task_count > 0:
            continue

        def _tx(conn):
            build_row = conn.execute(
                """
                SELECT summary_json
                FROM builds
                WHERE id=? AND status IN ('running','queued','pending')
                """,
                [build_id],
            ).fetchone()
            if build_row is None:
                return 0
            summary = _summary_object(build_row["summary_json"] if build_row is not None else None)
            summary["cancelled"] = True
            summary["cancel_reason"] = cancel_reason
            if not str(summary.get("error") or "").strip():
                summary["error"] = cancel_reason
            cursor = conn.execute(
                """
                UPDATE builds
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status IN ('running','queued','pending')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runs
                      WHERE build_id=?
                        AND status IN ('running','queued','pending')
                  )
                """,
                [json.dumps(summary), now_text, build_id, build_id],
            )
            try:
                return int(cursor.rowcount or 0)
            except Exception:
                return 0

        if int(config.db.write_transaction(_tx)) > 0:
            cancelled_count += 1
    return cancelled_count

def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    execute_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    workspace_id = int(ctx['workspace']['id'])
    requested_invocation_id = parse_run_detail_invocation_id(request)
    requested_detail_ids = parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            workspace_id,
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids
    if requested_invocation_id or requested_detail_ids:
        detail_ctx = build_run_detail_context(
            ctx,
            detail_scope_run_ids,
            execute_mode,
            requested_invocation_id=requested_invocation_id,
        )
        cancel_invocation_id = requested_invocation_id or _detail_invocation_id(detail_ctx)
        detail_ctx["cancel_invocation_id"] = cancel_invocation_id
        detail_ctx["cancel_available"] = bool(cancel_invocation_id and detail_ctx.get("detail_running"))
        detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
        detail_ctx["detail_table_compact"] = detail_table_compact
        detail_page_ctx = dict(ctx)
        detail_page_ctx['page_wide_content'] = detail_table_compact
        detail_page_ctx['topbar_max_1400'] = detail_table_compact
        return template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})
    runs = run_list_rows(int(ctx['problem']['id']), workspace_id, workspace, limit=10, actor_user_id=int(ctx['user']['id']))
    return template_response(request, 'run.html', {'ctx': ctx, 'runs': runs})

def run_new_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    active_build = latest_workspace_build(int(ctx['problem']['id']), workspace_id, ok_only=True)
    solution_options, default_submission_path, solution_options_truncated = run_solution_options_context(workspace)
    test_options, test_options_truncated, test_options_source = run_test_options_context(problem, workspace, active_build)
    selected_solution_paths: list[str] = []
    for raw in request.query_params.getlist('solution_paths'):
        normalized = normalize_optional_component_source_path_safe(raw, 'solutions', 'solution path')
        if normalized:
            selected_solution_paths.append(normalized)
    selected_solution_paths = dedupe_preserve_order(selected_solution_paths)
    rerun_invocation_id = normalize_run_id_token(request.query_params.get("rerun_invocation_id"))
    force_recompile = str(request.query_params.get("force_recompile") or "").strip().lower() in {"1", "true", "yes", "on"}
    if (not selected_solution_paths) and rerun_invocation_id:
        selected_solution_paths = _rerun_solution_paths_from_invocation(
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            actor_user_id=int(ctx["user"]["id"]),
            workspace=workspace,
            invocation_id=rerun_invocation_id,
        )
    if not selected_solution_paths and default_submission_path:
        selected_solution_paths = [default_submission_path]
    selected_test_names_raw = request.query_params.getlist('test_names')
    selected_test_names = parse_run_test_names(selected_test_names_raw)
    allowed_test_names = {str(row.get('name') or '') for row in test_options}
    selected_test_names = [name for name in selected_test_names if name in allowed_test_names]
    if not selected_test_names_raw and test_options and (not test_options_truncated):
        selected_test_names = [str(row.get('name') or '') for row in test_options if str(row.get('name') or '').strip()]
    return template_response(
        request,
        'run_execute.html',
        {
            'ctx': ctx,
            'solution_options': solution_options,
            'solution_options_truncated': solution_options_truncated,
            'selected_solution_paths': selected_solution_paths,
            'test_options': test_options,
            'test_options_truncated': test_options_truncated,
            'test_options_source': test_options_source,
            'selected_test_names': selected_test_names,
            'force_recompile': bool(force_recompile),
        },
    )

def run_details_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    execute_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    requested_invocation_id = parse_run_detail_invocation_id(request)
    requested_detail_ids = parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            int(ctx['workspace']['id']),
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids
    detail_ctx = build_run_detail_context(
        ctx,
        detail_scope_run_ids,
        execute_mode,
        requested_invocation_id=requested_invocation_id,
    )
    cancel_invocation_id = requested_invocation_id or _detail_invocation_id(detail_ctx)
    detail_ctx["cancel_invocation_id"] = cancel_invocation_id
    detail_ctx["cancel_available"] = bool(cancel_invocation_id and detail_ctx.get("detail_running"))
    detail_table_compact = _run_detail_use_compact_layout(detail_ctx)
    detail_ctx["detail_table_compact"] = detail_table_compact
    detail_page_ctx = dict(ctx)
    detail_page_ctx['page_wide_content'] = detail_table_compact
    detail_page_ctx['topbar_max_1400'] = detail_table_compact
    return template_response(request, 'run_details.html', {'ctx': detail_page_ctx, **detail_ctx})

def run_details_test_fragment(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    execute_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))

    requested_invocation_id = parse_run_detail_invocation_id(request)
    requested_detail_ids = parse_run_detail_ids(request)
    detail_scope_run_ids: list[str] = []
    if requested_invocation_id:
        detail_scope_run_ids = run_invocation_scope_run_ids(
            int(ctx['problem']['id']),
            int(ctx['workspace']['id']),
            requested_invocation_id,
            actor_user_id=int(ctx['user']['id']),
        )
    elif requested_detail_ids:
        detail_scope_run_ids = requested_detail_ids

    test_name = normalize_run_test_name_token(request.query_params.get('test'))
    if not test_name:
        raise HTTPException(status_code=400, detail='test is required')

    detail_ctx = build_run_detail_context(
        ctx,
        detail_scope_run_ids,
        execute_mode,
        requested_invocation_id=requested_invocation_id,
        include_row_details=True,
        detail_test_name=test_name,
    )
    detail_rows = detail_ctx.get('detail_rows')
    if not isinstance(detail_rows, list) or not detail_rows:
        raise HTTPException(status_code=404, detail='test detail not found')
    row = detail_rows[0] if isinstance(detail_rows[0], dict) else None
    if row is None:
        raise HTTPException(status_code=404, detail='test detail not found')
    detail_columns = detail_ctx.get('detail_columns')
    if not isinstance(detail_columns, list):
        detail_columns = []
    response = config.templates.TemplateResponse(
        request,
        '_run_test_detail_fragment.html',
        {
            'ctx': ctx,
            'row': row,
            'detail_columns': detail_columns,
        },
    )
    return response

def run_cancel(problem: str, user: str, invocation_id: str = Form("")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    require_write_access(ctx)
    safe_invocation_id = normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return redirect_response(
            f"/problems/{problem}/{user}/run",
            status_code=303,
            message="verification id is required",
        )
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    invocation_run_ids = run_invocation_scope_run_ids(
        problem_id,
        workspace_id,
        safe_invocation_id,
        actor_user_id=actor_user_id,
    )
    invocation_run_ids = dedupe_preserve_order(
        [normalize_run_id_token(item) for item in invocation_run_ids if normalize_run_id_token(item)]
    )
    details_url = f"/problems/{problem}/{user}/run/details?invocation_id={quote_plus(safe_invocation_id)}"
    if not invocation_run_ids:
        return redirect_response(details_url, status_code=303, message="verification not found")
    reason = "verification cancelled by user"
    try:
        source_hints = run_source_labels_from_audit(
            problem_id,
            actor_user_id,
            invocation_run_ids,
            limit=max(240, len(invocation_run_ids) * 8),
        )
    except Exception:
        source_hints = {}

    cancelled_runs = 0
    for run_token in invocation_run_ids:
        row = config.db.fetch_one(
            "SELECT status,build_id,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?",
            [run_token, problem_id, workspace_id],
        )
        status_text = str(row["status"] or "").strip().lower() if row is not None else "missing"
        if status_text in {"ok", "failed"}:
            summary_done = _summary_object(row["summary_json"] if row is not None else None)
            if not bool(summary_done.get("cancelled")):
                continue
        build_id = str(row["build_id"] or "").strip() if row is not None else ""
        if not build_id:
            build_id = str(_C.RUN_PLACEHOLDER_BUILD_ID)
        summary_obj = _summary_object(row["summary_json"] if row is not None else None)
        source_label = str(summary_obj.get("source") or "").strip()
        if not source_label:
            source_label = str(source_hints.get(run_token) or "").strip() or "verification"
        if row is None:
            record_async_run_failure(
                problem,
                user,
                run_token,
                mode="pass-fail",
                source_label=source_label,
                error=reason,
                build_id=build_id,
                invocation_id=safe_invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior="unknown",
                invocation_source="run.execute",
                synthesize_failed_tests=False,
                failure_stage="cancel",
                execution_skipped=True,
            )
        _mark_run_cancelled(run_token, reason)
        cancelled_runs += 1

    cancelled_tasks = _cancel_judgehost_tasks(invocation_run_ids, reason)
    cancelled_builds = _finalize_cancelled_builds(invocation_run_ids, reason)
    cancel_details: dict[str, object] = {
        "invocation_id": safe_invocation_id,
        "run_ids": invocation_run_ids,
        "run_count": len(invocation_run_ids),
        "cancelled_runs": cancelled_runs,
        "cancelled_tasks": cancelled_tasks,
        "cancelled_builds": cancelled_builds,
        "reason": reason,
    }
    audit(actor_user_id, problem_id, "run.cancel", cancel_details)
    if cancelled_runs > 0 or cancelled_tasks > 0:
        msg = f"cancel requested ({cancelled_runs}/{len(invocation_run_ids)} runs)"
    else:
        msg = "verification already finished"
    return redirect_response(details_url, status_code=303, message=msg)

def run_execute(
    problem: str,
    user: str,
    build_id: str = Form(""),
    solution_paths: list[str] = Form(default=[]),
    test_names: list[str] = Form(default=[]),
    submission_upload: UploadFile | None = File(None),
    force_recompile: str = Form(""),
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    run_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    solution_options, _, _ = run_solution_options_context(workspace)
    solution_expected_map: dict[str, str] = {}
    for row in solution_options:
        path = str(row.get('path') or '').strip()
        if not path:
            continue
        solution_expected_map[path] = normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))
    upload_content = None
    upload_filename = ''
    uploaded = False
    force_recompile_flag = str(force_recompile or '').strip().lower() in {'1', 'true', 'yes', 'on'}
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
            selected_solution_paths.append(normalize_optional_component_source_path(token, 'solutions', 'solution path'))
        selected_solution_paths = dedupe_preserve_order(selected_solution_paths)
        selected_test_names = parse_run_test_names(test_names)
        execution_targets: list[tuple[str | None, bool]] = []
        execution_targets.extend(((path, False) for path in selected_solution_paths))
        if uploaded:
            execution_targets.append((None, True))
        if not execution_targets:
            msg = 'select at least one solution or upload source file'
            return redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        deduped_targets: list[tuple[str | None, bool]] = []
        seen_targets: set[tuple[str, bool]] = set()
        for target_submission_path, target_is_upload in execution_targets:
            key = (str(target_submission_path or ''), bool(target_is_upload))
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped_targets.append((target_submission_path, target_is_upload))
        execution_targets = deduped_targets
        use_local_compile_check = False
        if use_local_compile_check and uploaded and isinstance(upload_content, (bytes, bytearray)):
            compile_check_error = upload_compile_check_error(
                workspace,
                upload_filename,
                bytes(upload_content),
                compile_program=config.toolchain_service.compile_program,
                cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS),
            )
            if compile_check_error:
                msg = f'upload compile check failed: {compile_check_error}'
                return redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        if use_local_compile_check:
            for target_submission_path, target_is_upload in execution_targets:
                if target_is_upload:
                    continue
                solution_path = str(target_submission_path or '').strip()
                if not solution_path:
                    continue
                compile_check_error = workspace_source_compile_check_error(
                    workspace,
                    solution_path,
                    compile_program=config.toolchain_service.compile_program,
                    cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS),
                )
                if compile_check_error:
                    msg = f'compile check failed: {compile_check_error}'
                    return redirect_response(f'/problems/{problem}/{user}/run/new', status_code=303, message=msg)
        requested_build_id = str(build_id or '').strip()
        if requested_build_id:
            assert_workspace_build_access(ctx, requested_build_id)
        run_ids: list[str] = []
        resolved_submission_paths: list[str] = []
        background_targets: list[dict[str, object]] = []
        invocation_id = allocate_invocation_id()
        for target_submission_path, target_is_upload in execution_targets:
            run_id = allocate_run_id()
            run_ids.append(run_id)
            source_label = target_submission_path or upload_filename or 'upload'
            expected_behavior = 'unknown'
            if target_submission_path:
                resolved_submission_paths.append(target_submission_path)
                expected_behavior = solution_expected_map.get(target_submission_path, 'unknown')
                if expected_behavior == 'unknown':
                    safe_solution = normalize_optional_component_source_path_safe(target_submission_path, 'solutions', 'solution path')
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
            'force_recompile': bool(force_recompile_flag),
        }
        audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', run_execute_details)
        try:
            started = start_run_execute_batch(
                problem,
                user,
                requested_build_id=requested_build_id,
                run_mode=run_mode,
                targets=background_targets,
                invocation_id=invocation_id,
                invocation_run_ids=run_ids,
                selected_test_names=selected_test_names,
                force_recompile=bool(force_recompile_flag),
            )
        except Exception as exc:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = str(exc)
            audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=str(exc))
        if not started:
            failed_details = dict(run_execute_details)
            failed_details['status'] = 'failed'
            failed_details['error'] = 'verification queue rejected'
            audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message='verification queue rejected')
        message_parts: list[str] = []
        if selected_test_names:
            message_parts.append(f'tests selected ({len(selected_test_names)})')
        message_parts.append(f'verification running ({len(run_ids)} programs)')
        message_text = '; '.join(message_parts)
        if primary_run_id:
            return redirect_response(
                f'/problems/{problem}/{user}/run/details?invocation_id={quote_plus(invocation_id)}',
                status_code=303,
                message=message_text,
            )
        return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=message_text)
    finally:
        if submission_upload is not None:
            submission_upload.file.close()


