from __future__ import annotations
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile

from app.db import now_iso
from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_job import start_verification_job
from app.impl.workspace.context_ui import page_ctx
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
from app.impl.workspace.context_verification import latest_workspace_verification, normalize_run_id_token
from app.impl.workspace.problem_config import read_problem_config
from app.impl.workspace.run_view_detail import build_run_detail_context
from app.impl.workspace.run_view_list import run_list_rows
from app.main_util import normalize_optional_component_source_path, normalize_optional_component_source_path_safe
from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior
from app.impl.run_export.query import (
    _rerun_solution_paths_from_verification,
    _run_detail_use_compact_layout,
)
from app.service.verification.task_scheduler import notify_verification_cancelled
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import Status

_C = config.constants


def _empty_task_counts() -> dict[str, object]:
    return {
        "total": 0,
        "pending": 0,
        "queued": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "by_kind": {
            "generate-input": {"pending": 0, "queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
            "main-correct": {"pending": 0, "queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
            "solution-run": {"pending": 0, "queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
        },
    }


def _upload_filename_token(raw: str) -> str:
    token = Path(str(raw or "").strip()).name
    if token:
        return token
    return "upload.cpp"


def _uploaded_target_path(run_id: str, upload_filename: str) -> str:
    return f"uploads/{run_id}/{_upload_filename_token(upload_filename)}"


def _verification_record_run_ids(verification_id: str) -> list[str]:
    values: list[str] = []
    task_store = VerificationTaskStore(config.db)
    for row in task_store.list_rows(verification_id):
        run_id = normalize_run_id_token(row.get("run_id"))
        if run_id and run_id not in values:
            values.append(run_id)
    return values

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
    active_verification = latest_workspace_verification(int(ctx['problem']['id']), workspace_id, ok_only=True)
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
    task_store = VerificationTaskStore(config.db)
    config.verification_service.cancel_verification_if_active(
        safe_verification_id,
        reason=reason,
        now_text=now_iso(),
    )
    notify_verification_cancelled(safe_verification_id, reason)
    cancelled_tasks = _cancel_judgehost_tasks(verification_run_ids, reason)
    run_progress = config.judgehost_task_service.domjudge_case_progress_for_runs(verification_run_ids)
    protected_run_ids = {
        run_id
        for run_id, progress in run_progress.items()
        if int(progress.get("total") or 0) > int(progress.get("reported") or 0)
    }
    task_store.cancel_not_started_tasks(
        safe_verification_id,
        reason=reason,
        protected_run_ids=protected_run_ids,
    )
    cancel_details: dict[str, object] = {
        "verification_id": safe_verification_id,
        "run_ids": verification_run_ids,
        "run_count": len(verification_run_ids),
        "cancelled_runs": 0,
        "cancelled_tasks": cancelled_tasks,
        "reason": reason,
    }
    problem_id = int(ctx["problem"]["id"])
    audit(actor_user_id, problem_id, "run.cancel", cancel_details)
    if cancelled_tasks > 0:
        msg = f"cancel requested ({cancelled_tasks} active tasks)"
    else:
        msg = "verification cancelled"
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
    solution_options, accepted_solution_path, _ = run_solution_options_context(workspace)
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
        run_ids: list[str] = []
        resolved_submission_paths: list[str] = []
        verification_id = allocate_verification_id()
        if not accepted_solution_path:
            raise ValueError("main correct solution is required")
        dag_targets: list[dict[str, object]] = []
        target_paths = list(selected_solution_paths)
        if accepted_solution_path not in target_paths:
            target_paths = [accepted_solution_path, *target_paths]
        for target_path in target_paths:
            run_id = allocate_run_id()
            run_ids.append(run_id)
            expected_behavior = solution_expected_map.get(target_path, "unknown")
            if target_path == accepted_solution_path:
                expected_behavior = "accepted"
            if expected_behavior == "unknown":
                expected_behavior = infer_expected_behavior_from_name(target_path)
            dag_targets.append(
                {
                    "path": target_path,
                    "expected_behavior": normalize_expected_behavior(expected_behavior),
                    "run_id": run_id,
                }
            )
            resolved_submission_paths.append(target_path)
        if uploaded:
            uploaded_run_id = allocate_run_id()
            run_ids.append(uploaded_run_id)
            dag_targets.append(
                {
                    "path": _uploaded_target_path(uploaded_run_id, upload_filename),
                    "expected_behavior": normalize_expected_behavior(infer_expected_behavior_from_name(upload_filename)),
                    "run_id": uploaded_run_id,
                    "upload_filename": _upload_filename_token(upload_filename),
                    "upload_content": bytes(upload_content or b""),
                }
            )
        primary_run_id = run_ids[0]
        workspace_head = str(ctx["workspace"].get("head_commit") or "")
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        run_execute_details: dict[str, object] = {
            "verification_id": verification_id,
            "run_id": primary_run_id,
            "run_ids": run_ids,
            "run_count": len(run_ids),
            "artifact_verification_id": verification_id,
            "submission_paths": resolved_submission_paths,
            "solution_paths": selected_solution_paths,
            "selected_test_names": selected_test_names,
            "uploaded": uploaded,
            "upload_filename": _upload_filename_token(upload_filename) if uploaded else "",
            "mode": run_mode,
            "execution_model": "task-dag",
            "async": True,
            "status": Status.QUEUED.value,
            "force_recompile": force_recompile_flag,
        }
        audit(ctx["user"]["id"], ctx["problem"]["id"], "run.execute", run_execute_details)
        try:
            started = start_verification_job(
                problem,
                user,
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
                targets=dag_targets,
                verification_id=verification_id,
                initial_details=run_execute_details,
                workspace_path=workspace,
                selected_test_names=selected_test_names,
            )
        except Exception as exc:
            failed_details = dict(run_execute_details)
            failed_details["status"] = Status.FAILED.value
            failed_details["error"] = str(exc)
            audit(ctx["user"]["id"], ctx["problem"]["id"], "run.execute", failed_details)
            return redirect_response(f"/problems/{problem}/{user}/run", status_code=303, message=str(exc))
        if not started:
            failed_details = dict(run_execute_details)
            failed_details["status"] = Status.FAILED.value
            failed_details["error"] = "verification already running"
            audit(ctx["user"]["id"], ctx["problem"]["id"], "run.execute", failed_details)
            return redirect_response(f"/problems/{problem}/{user}/run", status_code=303, message="verification already running")
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

