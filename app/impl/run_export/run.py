from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile

from app.db import now_iso
from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.artifact import assert_workspace_verification_access
from app.impl.workspace.context_job import page_ctx, start_run_execute_batch
from app.impl.workspace.context_job_helper import allocate_run_id, allocate_verification_id
from app.impl.workspace.context_operation import (
    audit,
    dedupe_preserve_order,
    run_solution_options_context,
    run_test_options_context,
)
from app.impl.workspace.context_run_detail import (
    normalize_run_test_name_token,
    parse_run_test_names,
    parse_verification_detail_id,
)
from app.impl.workspace.context_verification import latest_workspace_stage_verification, normalize_run_id_token
from app.impl.workspace.problem_config import read_problem_config
from app.impl.workspace.run_view_detail import build_run_detail_context
from app.impl.workspace.run_view_list import run_list_rows
from app.main_util import normalize_optional_component_source_path, normalize_optional_component_source_path_safe
from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior
from app.impl.run_export.query import (
    _rerun_solution_paths_from_verification,
    _run_detail_use_compact_layout,
    _summary_object,
)
from app.service.verification.store import (
    load_verification_run,
    load_verification_record,
    load_verification_summary,
    verification_run_ids as verification_summary_run_ids,
    save_verification_run_summary,
)
from app.service.verification.types import ACTIVE, Kind, Status

_C = config.constants


def _verification_record_run_ids(verification_id: str) -> list[str]:
    summary = load_verification_summary(config.db, verification_id)
    return verification_summary_run_ids(summary)
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
    if run_row is None:
        return
    summary = dict(run_row.get("summary") or {})
    cancel_reason = reason or ""
    summary["cancelled"] = True
    summary["cancel_reason"] = cancel_reason
    if not summary.get("error"):
        summary["error"] = cancel_reason
    mode = summary.get("mode") or verification_summary.get("mode") or "pass-fail"
    verification_source = verification_summary.get("verification_source") or "run.execute"
    source_label = summary.get("source") or safe_run_id
    source_paths = verification_summary.get("source_paths") or ([source_label] if source_label else [])
    verification_kind = verification_row.get("kind") or Kind.VERIFICATION.value
    expected_behavior = normalize_expected_behavior(run_row.get("expected_behavior") or summary.get("expected_behavior"))
    artifact_path = run_row.get("artifact_path") or ""
    error_text = summary.get("error") or ""
    save_verification_run_summary(
        config.db,
        config.fs_manager,
        verification_id=safe_verification_id,
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        kind=verification_kind,
        mode=mode,
        verification_source=verification_source,
        source_paths=source_paths,
        run_id=safe_run_id,
        run_status=Status.FAILED,
        source_label=source_label,
        expected_behavior=expected_behavior,
        run_summary=summary,
        artifact_path=artifact_path,
        error_text=error_text,
        finished=True,
    )

def _cancel_judgehost_tasks(run_ids: list[str], reason: str) -> int:
    safe_ids: list[str] = []
    for item in run_ids:
        token = normalize_run_id_token(item)
        if token:
            safe_ids.append(token)
    safe_ids = dedupe_preserve_order(safe_ids)
    service = getattr(config, "judgehost_task_service", None)
    affected = 0
    if not safe_ids:
        return affected
    cancel_reason = reason or "verification cancelled by user"
    if service is not None:
        try:
            affected = int(service.cancel_tasks_for_runs(safe_ids, reason=cancel_reason))
        except Exception:
            affected = 0

    if service is not None:
        try:
            service.cancel_domjudge_jobs_for_runs(safe_ids, final_status=Status.FAILED.value)
        except Exception:
            pass

    return affected

def _finalize_cancelled_verifications(verification_ids: list[str], reason: str) -> int:
    safe_verification_ids = dedupe_preserve_order(
        [
            token
            for item in verification_ids
            if (token := normalize_run_id_token(item))
        ]
    )
    safe_verification_ids = [token for token in safe_verification_ids if token != _C.RUN_PLACEHOLDER_VERIFICATION_ID]
    if not safe_verification_ids:
        return 0
    now_text = now_iso()
    cancelled_count = 0
    service = getattr(config, "judgehost_task_service", None)
    cancel_reason = reason or "verification cancelled by user"
    for verification_id in safe_verification_ids:
        active_task_count = 0
        if service is not None:
            try:
                active_task_count = int(service.active_task_count_for_verification(verification_id))
            except Exception:
                active_task_count = 0
        if active_task_count > 0:
            continue
        if config.verification_service.cancel_verification_if_active(
            verification_id,
            reason=cancel_reason,
            now_text=now_text,
        ):
            cancelled_count += 1
    return cancelled_count

def run_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    execute_mode = general_cfg['mode']
    workspace_id = int(ctx['workspace']['id'])
    requested_verification_id = parse_verification_detail_id(request)
    if requested_verification_id:
        detail_ctx = build_run_detail_context(
            ctx,
            execute_mode,
            requested_verification_id=requested_verification_id,
        )
        cancel_verification_id = requested_verification_id or detail_ctx["verification_id"]
        detail_ctx["cancel_verification_id"] = cancel_verification_id
        detail_ctx["cancel_available"] = bool(cancel_verification_id and detail_ctx["detail_running"])
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
    rerun_verification_id_value = request.query_params.get("verification_id")
    if not rerun_verification_id_value:
        rerun_verification_id_value = request.query_params.get("rerun_verification_id")
    rerun_verification_id = normalize_run_id_token(rerun_verification_id_value)
    force_recompile_param = request.query_params.get("force_recompile", "")
    force_recompile = force_recompile_param.lower() in {"1", "true", "yes", "on"}
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
    allowed_test_names = {row["name"] for row in test_options}
    selected_test_names = [name for name in selected_test_names if name in allowed_test_names]
    if not selected_test_names_raw and test_options and (not test_options_truncated):
        selected_test_names = [row["name"] for row in test_options]
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
            'force_recompile': force_recompile,
        },
    )

def run_details_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=True, include_workspace_changes=True)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    execute_mode = general_cfg['mode']
    requested_verification_id = parse_verification_detail_id(request)
    detail_ctx = build_run_detail_context(
        ctx,
        execute_mode,
        requested_verification_id=requested_verification_id,
    )
    cancel_verification_id = requested_verification_id or detail_ctx["verification_id"]
    detail_ctx["cancel_verification_id"] = cancel_verification_id
    detail_ctx["cancel_available"] = bool(cancel_verification_id and detail_ctx["detail_running"])
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
    execute_mode = general_cfg['mode']

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
    detail_rows = detail_ctx['detail_rows']
    if not detail_rows:
        raise HTTPException(status_code=404, detail='test detail not found')
    row = detail_rows[0]
    detail_columns = detail_ctx['detail_columns']
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

def run_cancel(problem: str, user: str, verification_id: Annotated[str, Form()] = ""):
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
    verification_run_ids = _verification_record_run_ids(safe_verification_id)
    normalized_run_ids: list[str] = []
    for item in verification_run_ids:
        token = normalize_run_id_token(item)
        if token:
            normalized_run_ids.append(token)
    verification_run_ids = dedupe_preserve_order(normalized_run_ids)
    details_url = f"/problems/{problem}/{user}/run/details?verification_id={quote_plus(safe_verification_id)}"
    if not verification_run_ids:
        return redirect_response(details_url, status_code=303, message="verification not found")
    reason = "verification cancelled by user"
    cancelled_runs = 0
    verification_summary = load_verification_summary(config.db, safe_verification_id)
    artifact_verification_ids: list[str] = []
    top_artifact_verification_id = verification_summary.get("artifact_verification_id") or ""
    if top_artifact_verification_id:
        artifact_verification_ids.append(top_artifact_verification_id)
    for run_token in verification_run_ids:
        run_row = load_verification_run(
            config.db,
            verification_id=safe_verification_id,
            run_id=run_token,
        )
        if run_row is None:
            continue
        summary_obj = dict(run_row.get("summary") or {})
        status_text = run_row["status"] or "missing"
        if status_text in {Status.OK.value, Status.FAILED.value}:
            if not bool(summary_obj.get("cancelled")):
                continue
        artifact_verification_id = summary_obj.get("artifact_verification_id") or ""
        if not artifact_verification_id:
            artifact_verification_id = _C.RUN_PLACEHOLDER_VERIFICATION_ID
        source_label = summary_obj.get("source") or ""
        if not source_label:
            source_label = run_row.get("source_label") or ""
        if not source_label:
            source_label = "verification"
        if (
            artifact_verification_id
            and artifact_verification_id != _C.RUN_PLACEHOLDER_VERIFICATION_ID
            and artifact_verification_id not in artifact_verification_ids
        ):
            artifact_verification_ids.append(artifact_verification_id)
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
    artifact_verification_id: Annotated[str, Form()] = "",
    solution_paths: Annotated[list[str], Form()] = [],
    test_names: Annotated[list[str], Form()] = [],
    submission_upload: Annotated[UploadFile | None, File()] = None,
    force_recompile: Annotated[str, Form()] = "",
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False, include_workspace_changes=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    _, general_cfg, _ = read_problem_config(workspace)
    run_mode = general_cfg['mode']
    solution_options, _, _ = run_solution_options_context(workspace)
    solution_expected_map = {
        row["path"]: row["expected_behavior"]
        for row in solution_options
    }
    upload_content = None
    upload_filename = ''
    uploaded = False
    force_recompile_flag = force_recompile.lower() in {'1', 'true', 'yes', 'on'}
    try:
        if submission_upload is not None:
            normalized_name = (submission_upload.filename or '').strip()
            if normalized_name:
                upload_filename = normalized_name
                upload_content = submission_upload.file.read()
                uploaded = True
        selected_solution_paths = dedupe_preserve_order(
            [
                normalize_optional_component_source_path(path, 'solutions', 'solution path')
                for path in solution_paths
                if path
            ]
        )
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
            key = (target_submission_path or '', target_is_upload)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            deduped_targets.append((target_submission_path, target_is_upload))
        execution_targets = deduped_targets
        if artifact_verification_id:
            assert_workspace_verification_access(ctx, artifact_verification_id)
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
                    expected_behavior = infer_expected_behavior_from_name(target_submission_path)
            background_targets.append({'run_id': run_id, 'submission_path': target_submission_path or '', 'upload_content': upload_content if target_is_upload else None, 'upload_filename': upload_filename if target_is_upload else '', 'source_label': source_label, 'expected_behavior': expected_behavior})
        primary_run_id = run_ids[0]
        run_execute_details: dict[str, object] = {
            'verification_id': verification_id,
            'run_id': primary_run_id,
            'run_ids': run_ids,
            'run_count': len(run_ids),
            'artifact_verification_id': artifact_verification_id,
            'submission_paths': resolved_submission_paths,
            'solution_paths': selected_solution_paths,
            'selected_test_names': selected_test_names,
            'uploaded': uploaded,
            'mode': run_mode,
            'implicit_verification_generated': not artifact_verification_id,
            'verification_backend': config.judgehost_task_service.backend_name(),
            'async': True,
            'status': Status.QUEUED.value,
            'force_recompile': force_recompile_flag,
        }
        audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', run_execute_details)
        try:
            started = start_run_execute_batch(
                problem,
                user,
                requested_verification_id=artifact_verification_id,
                run_mode=run_mode,
                targets=background_targets,
                verification_id=verification_id,
                verification_run_ids=run_ids,
                selected_test_names=selected_test_names,
                force_recompile=force_recompile_flag,
            )
        except Exception as exc:
            failed_details = dict(run_execute_details)
            failed_details['status'] = Status.FAILED.value
            failed_details['error'] = str(exc)
            audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message=str(exc))
        if not started:
            failed_details = dict(run_execute_details)
            failed_details['status'] = Status.FAILED.value
            failed_details['error'] = 'verification queue rejected'
            audit(ctx['user']['id'], ctx['problem']['id'], 'run.execute', failed_details)
            return redirect_response(f'/problems/{problem}/{user}/run', status_code=303, message='verification queue rejected')
        message_parts: list[str] = []
        if selected_test_names:
            message_parts.append(f'tests selected ({len(selected_test_names)})')
        message_parts.append(f'verification running ({len(run_ids)} programs)')
        message_text = '; '.join(message_parts)
        return redirect_response(
            f'/problems/{problem}/{user}/run/details?verification_id={quote_plus(verification_id)}',
            status_code=303,
            message=message_text,
        )
    finally:
        if submission_upload is not None:
            submission_upload.file.close()
