from __future__ import annotations

from app.impl.run_export.context import (
    Path,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    _C,
    allocate_verification_id,
    allocate_run_id,
    assert_workspace_verification_access,
    audit,
    build_run_detail_context,
    dedupe_preserve_order,
    latest_workspace_stage_verification,
    normalize_optional_component_source_path,
    normalize_optional_component_source_path_safe,
    normalize_problem_mode,
    normalize_run_id_token,
    normalize_run_test_name_token,
    parse_verification_detail_id,
    parse_run_test_names,
    read_problem_config,
    record_async_run_failure,
    redirect_response,
    require_write_access,
    run_list_rows,
    run_solution_options_context,
    run_test_options_context,
    verification_record_run_ids,
    start_run_execute_batch,
    template_response,
    config,
    infer_expected_behavior_from_name,
    json,
    normalize_expected_behavior,
    now_iso,
    page_ctx,
    quote_plus,
)
from app.impl.run_export.query import (
    _detail_verification_id,
    _rerun_solution_paths_from_verification,
    _run_detail_use_compact_layout,
    _summary_object,
)
from app.service.verification import (
    VERIFICATION_KIND_VERIFICATION,
    load_verification_run,
    load_verification_record,
    load_verification_summary,
    save_verification_run_summary,
)
def _mark_run_cancelled(
    problem_id: int,
    workspace_id: int,
    verification_id: str,
    run_id: str,
    reason: str,
) -> None:
    safe_run_id = normalize_run_id_token(run_id)
    safe_verification_id = normalize_run_id_token(verification_id)
    if (not safe_run_id) or (not safe_verification_id):
        return
    verification_row_raw = load_verification_record(config.db, safe_verification_id)
    verification_row = dict(verification_row_raw) if verification_row_raw is not None else None
    if verification_row is None:
        return
    verification_summary = load_verification_summary(config.db, safe_verification_id)
    run_row = load_verification_run(
        config.db,
        verification_id=safe_verification_id,
        run_id=safe_run_id,
    )
    summary = {}
    if isinstance(run_row, dict):
        run_summary = run_row.get("summary")
        if isinstance(run_summary, dict):
            summary = dict(run_summary)
    summary["cancelled"] = True
    summary["cancel_reason"] = str(reason or "").strip()
    if not str(summary.get("error") or "").strip():
        summary["error"] = str(reason or "").strip()
    artifact_verification_id = str(summary.get("artifact_verification_id") or "").strip()
    mode = str(summary.get("mode") or verification_summary.get("mode") or "pass-fail").strip() or "pass-fail"
    verification_source = str(verification_summary.get("verification_source") or "run.execute").strip() or "run.execute"
    source_label = str(summary.get("source") or "").strip() or safe_run_id
    source_paths_obj = verification_summary.get("source_paths")
    source_paths = list(source_paths_obj) if isinstance(source_paths_obj, list) else ([source_label] if source_label else [])
    save_verification_run_summary(
        config.db,
        config.fs_manager,
        verification_id=safe_verification_id,
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        kind=str(verification_row.get("kind") or VERIFICATION_KIND_VERIFICATION).strip() or VERIFICATION_KIND_VERIFICATION,
        mode=mode,
        verification_source=verification_source,
        source_paths=source_paths,
        run_id=safe_run_id,
        run_status="failed",
        source_label=source_label,
        expected_behavior=str(run_row.get("expected_behavior") or summary.get("expected_behavior") or "unknown").strip() or "unknown",
        run_summary=summary,
        artifact_path=str(run_row.get("artifact_path") or "").strip(),
        error_text=str(summary.get("error") or "").strip(),
        finished=True,
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

def _finalize_cancelled_verifications(verification_ids: list[str], reason: str) -> int:
    safe_verification_ids = dedupe_preserve_order([str(item or "").strip() for item in verification_ids if str(item or "").strip()])
    safe_verification_ids = [token for token in safe_verification_ids if token != str(_C.RUN_PLACEHOLDER_BUILD_ID)]
    if not safe_verification_ids:
        return 0
    now_text = now_iso()
    cancelled_count = 0
    service = getattr(config, "judgehost_task_service", None)
    cancel_reason = str(reason or "").strip() or "verification cancelled by user"
    for verification_id in safe_verification_ids:
        active_task_count = 0
        if service is not None:
            try:
                active_task_count = int(service.active_task_count_for_verification(verification_id))
            except Exception:
                active_task_count = 0
        if active_task_count > 0:
            continue

        def _tx(conn):
            verification_row = conn.execute(
                """
                SELECT summary_json
                FROM verifications
                WHERE id=? AND kind='verification' AND status IN ('running','queued','pending')
                """,
                [verification_id],
            ).fetchone()
            if verification_row is None:
                return 0
            summary = _summary_object(verification_row["summary_json"] if verification_row is not None else None)
            summary["cancelled"] = True
            summary["cancel_reason"] = cancel_reason
            if not str(summary.get("error") or "").strip():
                summary["error"] = cancel_reason
            cursor = conn.execute(
                """
                UPDATE verifications
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND kind='verification' AND status IN ('running','queued','pending')
                """,
                [json.dumps(summary), now_text, verification_id],
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
    requested_verification_id = parse_verification_detail_id(request)
    if requested_verification_id:
        detail_ctx = build_run_detail_context(
            ctx,
            execute_mode,
            requested_verification_id=requested_verification_id,
        )
        cancel_verification_id = requested_verification_id or _detail_verification_id(detail_ctx)
        detail_ctx["cancel_verification_id"] = cancel_verification_id
        detail_ctx["cancel_available"] = bool(cancel_verification_id and detail_ctx.get("detail_running"))
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
    active_verification = latest_workspace_stage_verification(int(ctx['problem']['id']), workspace_id, ok_only=True)
    solution_options, default_submission_path, solution_options_truncated = run_solution_options_context(workspace)
    test_options, test_options_truncated, test_options_source = run_test_options_context(problem, workspace, active_verification)
    selected_solution_paths: list[str] = []
    for raw in request.query_params.getlist('solution_paths'):
        normalized = normalize_optional_component_source_path_safe(raw, 'solutions', 'solution path')
        if normalized:
            selected_solution_paths.append(normalized)
    selected_solution_paths = dedupe_preserve_order(selected_solution_paths)
    rerun_verification_id = normalize_run_id_token(
        request.query_params.get("verification_id")
        or request.query_params.get("rerun_verification_id")
    )
    force_recompile = str(request.query_params.get("force_recompile") or "").strip().lower() in {"1", "true", "yes", "on"}
    if (not selected_solution_paths) and rerun_verification_id:
        selected_solution_paths = _rerun_solution_paths_from_verification(
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            actor_user_id=int(ctx["user"]["id"]),
            workspace=workspace,
            verification_id=rerun_verification_id,
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
    requested_verification_id = parse_verification_detail_id(request)
    detail_ctx = build_run_detail_context(
        ctx,
        execute_mode,
        requested_verification_id=requested_verification_id,
    )
    cancel_verification_id = requested_verification_id or _detail_verification_id(detail_ctx)
    detail_ctx["cancel_verification_id"] = cancel_verification_id
    detail_ctx["cancel_available"] = bool(cancel_verification_id and detail_ctx.get("detail_running"))
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

    requested_verification_id = parse_verification_detail_id(request)

    test_name = normalize_run_test_name_token(request.query_params.get('test'))
    if not test_name:
        raise HTTPException(status_code=400, detail='test is required')

    detail_ctx = build_run_detail_context(
        ctx,
        execute_mode,
        requested_verification_id=requested_verification_id,
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

def run_cancel(problem: str, user: str, verification_id: str = Form("")):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    require_write_access(ctx)
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return redirect_response(
            f"/problems/{problem}/{user}/run",
            status_code=303,
            message="verification id is required",
        )
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    verification_run_ids = verification_record_run_ids(
        problem_id,
        workspace_id,
        safe_verification_id,
    )
    verification_run_ids = dedupe_preserve_order(
        [normalize_run_id_token(item) for item in verification_run_ids if normalize_run_id_token(item)]
    )
    details_url = f"/problems/{problem}/{user}/run/details?verification_id={quote_plus(safe_verification_id)}"
    if not verification_run_ids:
        return redirect_response(details_url, status_code=303, message="verification not found")
    reason = "verification cancelled by user"
    cancelled_runs = 0
    verification_summary = load_verification_summary(config.db, safe_verification_id)
    artifact_verification_ids: list[str] = []
    top_artifact_verification_id = str(
        verification_summary.get("artifact_verification_id")
        or ""
    ).strip() if isinstance(verification_summary, dict) else ""
    if top_artifact_verification_id:
        artifact_verification_ids.append(top_artifact_verification_id)
    for run_token in verification_run_ids:
        run_row = load_verification_run(
            config.db,
            verification_id=safe_verification_id,
            run_id=run_token,
        )
        run_summary = run_row.get("summary") if isinstance(run_row, dict) else None
        summary_obj = dict(run_summary) if isinstance(run_summary, dict) else {}
        status_text = str(run_row.get("status") or "").strip().lower() if isinstance(run_row, dict) else "missing"
        if status_text in {"ok", "failed"}:
            if not bool(summary_obj.get("cancelled")):
                continue
        artifact_verification_id = str(
            summary_obj.get("artifact_verification_id")
            or ""
        ).strip()
        if not artifact_verification_id:
            artifact_verification_id = str(_C.RUN_PLACEHOLDER_BUILD_ID)
        source_label = str(summary_obj.get("source") or "").strip()
        if not source_label:
            source_label = str(run_row.get("source_label") or "").strip() if isinstance(run_row, dict) else ""
        if not source_label:
            source_label = "verification"
        if (
            artifact_verification_id
            and artifact_verification_id != str(_C.RUN_PLACEHOLDER_BUILD_ID)
            and artifact_verification_id not in artifact_verification_ids
        ):
            artifact_verification_ids.append(artifact_verification_id)
        if not isinstance(run_row, dict) or not run_row:
            record_async_run_failure(
                problem,
                user,
                run_token,
                mode="pass-fail",
                source_label=source_label,
                error=reason,
                artifact_verification_id=artifact_verification_id,
                verification_id=safe_verification_id,
                expected_behavior="unknown",
                verification_source="run.execute",
                synthesize_failed_tests=False,
                failure_stage="cancel",
                execution_skipped=True,
            )
        _mark_run_cancelled(problem_id, workspace_id, safe_verification_id, run_token, reason)
        cancelled_runs += 1

    cancelled_tasks = _cancel_judgehost_tasks(verification_run_ids, reason)
    cancelled_verifications = _finalize_cancelled_verifications(artifact_verification_ids, reason)
    cancel_details: dict[str, object] = {
        "verification_id": safe_verification_id,
        "run_ids": verification_run_ids,
        "run_count": len(verification_run_ids),
        "cancelled_runs": cancelled_runs,
        "cancelled_tasks": cancelled_tasks,
        "cancelled_verifications": cancelled_verifications,
        "reason": reason,
    }
    audit(actor_user_id, problem_id, "run.cancel", cancel_details)
    if cancelled_runs > 0 or cancelled_tasks > 0:
        msg = f"cancel requested ({cancelled_runs}/{len(verification_run_ids)} runs)"
    else:
        msg = "verification already finished"
    return redirect_response(details_url, status_code=303, message=msg)

def run_execute(
    problem: str,
    user: str,
    artifact_verification_id: str = Form(""),
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
        requested_verification_id = str(artifact_verification_id or '').strip()
        if requested_verification_id:
            assert_workspace_verification_access(ctx, requested_verification_id)
        run_ids: list[str] = []
        resolved_submission_paths: list[str] = []
        background_targets: list[dict[str, object]] = []
        verification_id = allocate_verification_id()
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
            'verification_id': verification_id,
            'run_id': primary_run_id,
            'run_ids': run_ids,
            'run_count': len(run_ids),
            'artifact_verification_id': requested_verification_id,
            'submission_paths': resolved_submission_paths,
            'solution_paths': selected_solution_paths,
            'selected_test_names': selected_test_names,
            'uploaded': uploaded,
            'mode': run_mode,
            'implicit_verification_generated': not bool(requested_verification_id),
            'verification_backend': config.judgehost_task_service.backend_name(),
            'async': True,
            'status': 'queued',
            'force_recompile': bool(force_recompile_flag),
        }
        audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', run_execute_details)
        try:
            started = start_run_execute_batch(
                problem,
                user,
                requested_verification_id=requested_verification_id,
                run_mode=run_mode,
                targets=background_targets,
                verification_id=verification_id,
                verification_run_ids=run_ids,
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
                f'/problems/{problem}/{user}/run/details?verification_id={quote_plus(verification_id)}',
                status_code=303,
                message=message_text,
            )
        return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=message_text)
    finally:
        if submission_upload is not None:
            submission_upload.file.close()
