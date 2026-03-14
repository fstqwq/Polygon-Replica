from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import uuid

from app.db import now_iso
from app.impl.runtime.config import config

from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification import (
    VERIFICATION_KIND_VERIFICATION,
    create_verification_record,
    default_verification_summary,
    default_verification_run,
    load_verification_run,
    load_verification_summary,
    save_verification_summary,
    save_verification_run_summary,
    verification_stage_results,
    verification_update_lock,
)

from .artifact import assert_workspace_verification_access
from .context_operation import audit, dedupe_preserve_order, parse_summary_json
from .context_operation import list_solution_entries, resolve_build_accepted_solution_source
from .context_run_detail import normalize_run_id_token, normalize_run_test_name_token
from .context_ui import page_ctx
from .context_verification import (
    latest_workspace_committed_stage_verification,
    _verification_solution_failure_hint,
    _verification_solution_match,
    _verification_sources_signature,
    _verification_sources_signature_details,
)
from .export_dispatch import export_workspace_key
from .problem_config import normalize_problem_mode, read_problem_config
from .verification_dispatch import verification_workspace_key
from .context_job_helper import (
    VerificationFailureError,
    _allocate_run_id,
    annotate_verification_run_result,
    _ensure_implicit_verification,
    record_async_run_failure,
)

_C = config.constants
_BACKEND_NAME = config.judgehost_task_service.backend_name()


def _run_marked_cancelled(problem_id: int, workspace_id: int, run_id: str) -> bool:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return False
    verification_row, verification_summary = _load_execution_result(
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        verification_id=f'ver-{safe_run_id}',
        run_id=safe_run_id,
    )
    if verification_row is not None:
        status = str(verification_row.get('status') or '').strip().lower()
        if status == 'failed' and isinstance(verification_summary, dict):
            if bool(verification_summary.get('cancelled')):
                return True
            error_text = str(verification_summary.get('error') or '').strip().lower()
            if 'cancelled by user' in error_text:
                return True
    return False

def _verification_marked_cancelled(problem_id: int, actor_user_id: int, verification_id: str, *, limit: int = 240) -> bool:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return False
    rows = config.db.fetch_all(
        """
        SELECT details_json
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='run.cancel'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), max(40, int(limit))],
    )
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        if normalize_run_id_token(details.get('verification_id')) == safe_verification_id:
            return True
    return False

def _verification_submission_parallelism(target_count: int) -> int:
    safe_total = max(0, int(target_count))
    if safe_total <= 1:
        return 1
    host_count = 0
    fetch_batch_size = 1
    try:
        status = config.judgehost_task_service.status()
    except Exception:
        status = {}
    if isinstance(status, dict):
        try:
            hosts_online = max(0, int(status.get('hosts_online') or 0))
        except Exception:
            hosts_online = 0
        try:
            hosts_total = max(0, int(status.get('hosts_total') or 0))
        except Exception:
            hosts_total = 0
        host_count = hosts_online if hosts_online > 0 else hosts_total
        try:
            fetch_batch_size = max(1, int(status.get('fetch_batch_size') or 1))
        except Exception:
            fetch_batch_size = 1
    if host_count <= 0:
        host_count = 1
    estimate = max(1, host_count * fetch_batch_size)
    return max(1, min(safe_total, 32, estimate))


def _find_solve_main_run(
    problem_id: int,
    workspace_id: int,
    artifact_verification_id: str,
    accepted_source_path: str,
) -> tuple[dict[str, object] | None, dict | None]:
    safe_verification_id = str(artifact_verification_id or "").strip()
    safe_source = str(accepted_source_path or "").strip()
    if not safe_verification_id:
        return (None, None)
    row_raw = config.db.fetch_one(
        "SELECT id,status,summary_json,artifact_path,created_at,finished_at FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_verification_id, int(problem_id), int(workspace_id)],
    )
    row = dict(row_raw) if row_raw is not None else None
    if row is None:
        return (None, None)
    verification_summary = parse_summary_json(row.get("summary_json"), f"solve-main/{safe_verification_id}")
    if not isinstance(verification_summary, dict):
        return (None, None)
    stage_results_obj = verification_summary.get("stage_results")
    stage_results = stage_results_obj if isinstance(stage_results_obj, dict) else {}
    solve_stage = stage_results.get("solve_main")
    if not isinstance(solve_stage, dict):
        return (None, None)
    run_summary = dict(solve_stage)
    source_path = str(run_summary.get("source") or "").strip()
    if safe_source and source_path and source_path != safe_source:
        return (None, None)
    row_dict = {
        "id": f"solve-main-{safe_verification_id}",
        "artifact_verification_id": safe_verification_id,
        "kind": "verification",
        "status": str(run_summary.get("status") or row.get("status") or "").strip(),
        "summary_json": json.dumps(run_summary),
        "artifact_path": str(run_summary.get("artifact_path") or row.get("artifact_path") or ""),
        "created_at": str(row.get("created_at") or "").strip(),
        "finished_at": str(row.get("finished_at") or "").strip(),
    }
    return (row_dict, run_summary)


def _merge_verification_stage_context(
    *,
    problem_id: int,
    workspace_id: int,
    artifact_verification_id: str,
    verification_details: dict[str, object],
) -> None:
    safe_verification_id = str(artifact_verification_id or "").strip()
    if not safe_verification_id:
        verification_details.pop("stage_results", None)
        return
    verification_row = config.db.fetch_one(
        "SELECT status,summary_json FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_verification_id, int(problem_id), int(workspace_id)],
    )
    verification_summary = parse_summary_json(verification_row["summary_json"], f"verification/{safe_verification_id}") if verification_row is not None else {}
    if verification_row is not None:
        artifact_verification_status = str(verification_row["status"] or "").strip().lower()
        if artifact_verification_status:
            verification_details["artifact_verification_status"] = artifact_verification_status
    if isinstance(verification_summary, dict):
        verification_details["artifact_verification_error"] = str(verification_summary.get("error") or "").strip()
        verification_details["artifact_failed_step"] = str(verification_summary.get("failed_step") or "").strip()
        verification_details["artifact_failed_test"] = str(verification_summary.get("failed_test") or "").strip()
        stage_results = verification_stage_results(verification_summary)
        if stage_results:
            verification_details["stage_results"] = stage_results
        else:
            verification_details.pop("stage_results", None)


def _verification_generated_test_names(verification_details: dict[str, object]) -> list[str]:
    stage_results_obj = verification_details.get("stage_results")
    stage_results = stage_results_obj if isinstance(stage_results_obj, dict) else {}
    generate_stage = stage_results.get("generate_input")
    names: list[str] = []
    if isinstance(generate_stage, dict):
        tests_obj = generate_stage.get("tests")
        tests_rows = tests_obj if isinstance(tests_obj, list) else []
        for item in tests_rows:
            if not isinstance(item, dict):
                continue
            token = normalize_run_test_name_token(item.get("test"))
            if token and token not in names:
                names.append(token)
    return names


def _load_execution_result(
    *,
    problem_id: int,
    workspace_id: int,
    verification_id: str,
    run_id: str,
) -> tuple[dict[str, object] | None, dict | None]:
    safe_run_id = normalize_run_id_token(run_id)
    safe_verification_id = normalize_run_id_token(verification_id)
    if safe_verification_id and safe_run_id:
        run_row = load_verification_run(
            config.db,
            verification_id=safe_verification_id,
            run_id=safe_run_id,
        )
        if isinstance(run_row, dict) and run_row:
            run_summary = run_row.get("summary")
            summary_obj = dict(run_summary) if isinstance(run_summary, dict) else {}
            return (
                {
                    "id": safe_run_id,
                    "artifact_verification_id": str(summary_obj.get("artifact_verification_id") or "").strip(),
                    "mode": str(summary_obj.get("mode") or "").strip() or "pass-fail",
                    "status": str(run_row.get("status") or "running").strip().lower() or "running",
                    "summary_json": json.dumps(summary_obj),
                    "artifact_path": str(run_row.get("artifact_path") or "").strip(),
                    "created_at": "",
                    "finished_at": str(summary_obj.get("finished_at") or "").strip(),
                },
                summary_obj,
            )
    return (None, {})


def _seed_verification_runs(
    *,
    problem_id: int,
    workspace_id: int,
    verification_id: str,
    artifact_verification_id: str,
    mode: str,
    verification_source: str,
    targets: list[dict[str, object]],
    default_task_kind: str,
) -> None:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return
    safe_artifact_verification_id = str(artifact_verification_id or "").strip() or _C.RUN_PLACEHOLDER_BUILD_ID
    safe_mode = str(mode or "").strip() or "pass-fail"
    safe_source = str(verification_source or "").strip() or "run.execute"
    for target in targets:
        run_id = normalize_run_id_token(target.get("run_id"))
        if not run_id:
            continue
        existing = load_verification_run(
            config.db,
            verification_id=safe_verification_id,
            run_id=run_id,
        )
        existing_status = str(existing.get("status") or "").strip().lower() if isinstance(existing, dict) else ""
        if existing_status and existing_status not in {"queued", "pending", "running"}:
            continue
        source_label = (
            str(target.get("source_label") or "").strip()
            or str(target.get("path") or "").strip()
            or str(target.get("submission_path") or "").strip()
            or run_id
        )
        expected_behavior = normalize_expected_behavior(str(target.get("expected_behavior") or "unknown"))
        task_kind = str(target.get("task_kind") or default_task_kind or "").strip()
        run_root = config.fs_manager.prepare_verification_run_root(safe_verification_id, run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        save_verification_run_summary(
            config.db,
            config.fs_manager,
            verification_id=safe_verification_id,
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            kind=VERIFICATION_KIND_VERIFICATION,
            mode=safe_mode,
            verification_source=safe_source,
            source_paths=[source_label] if source_label else [],
            run_id=run_id,
            run_status=existing_status or "queued",
            source_label=source_label,
            expected_behavior=expected_behavior,
            run_summary={
                "artifact_verification_id": safe_artifact_verification_id,
                "mode": safe_mode,
                "source": source_label,
                "tests": [],
                "compile_diagnostics": [],
                "error": "",
            },
            artifact_path=str(run_root),
            task_kind=task_kind,
            finished=False,
        )


def _materialize_reused_buildsolve_run(
    *,
    problem_id: int,
    workspace_id: int,
    run_id: str,
    mode: str,
    buildsolve_row: dict[str, object],
    buildsolve_summary: dict,
    verification_id: str,
    verification_run_ids: list[str],
    expected_behavior: str,
) -> tuple[dict[str, object] | None, dict | None]:
    safe_run_id = normalize_run_id_token(run_id)
    if not safe_run_id:
        return (None, None)
    safe_mode = str(mode or "").strip() or "pass-fail"
    finished_at = str(buildsolve_row.get("finished_at") or "").strip()
    status = str(buildsolve_row.get("status") or "running").strip() or "running"
    verification_id = normalize_run_id_token(verification_id) or f"ver-{safe_run_id}"
    safe_artifact_verification_id = str(buildsolve_row.get("artifact_verification_id") or "").strip()
    save_verification_run_summary(
        config.db,
        config.fs_manager,
        verification_id=verification_id,
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        kind=VERIFICATION_KIND_VERIFICATION,
        mode=safe_mode,
        verification_source="verification.solve-main",
        source_paths=[str(buildsolve_summary.get("source") or "").strip()] if str(buildsolve_summary.get("source") or "").strip() else [],
        run_id=safe_run_id,
        run_status=status,
        source_label=str(buildsolve_summary.get("source") or "").strip() or safe_run_id,
        expected_behavior=expected_behavior,
        run_summary=dict(buildsolve_summary),
        artifact_path=str(buildsolve_row.get("artifact_path") or ""),
        task_kind="solve",
        error_text=str(buildsolve_summary.get("error") or "").strip(),
        finished=status.lower() not in {"queued", "pending", "running"},
    )
    annotate_verification_run_result(
        problem_id,
        workspace_id,
        safe_run_id,
        verification_id=verification_id,
        expected_behavior=expected_behavior,
        verification_source="verification.start",
    )
    return _load_execution_result(
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        verification_id=verification_id,
        run_id=safe_run_id,
    )

def _run_execute_batch_worker(
    problem: str,
    user: str,
    *,
    requested_verification_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    verification_id: str,
    verification_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> None:
    resolved_verification_id = str(requested_verification_id or '').strip()
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
        problem_id = int(ctx['problem']['id'])
        workspace_id = int(ctx['workspace']['id'])
        if not resolved_verification_id:
            try:
                resolved_verification_id, _ = _ensure_implicit_verification(problem, user, ctx=ctx, force=False)
            except VerificationFailureError as build_exc:
                resolved_verification_id = str(build_exc.verification_id or '').strip() or resolved_verification_id
                raise
        if not resolved_verification_id:
            raise RuntimeError('tests generation did not produce a runnable build')
        assert_workspace_verification_access(ctx, resolved_verification_id)
    except Exception as exc:
        err = str(exc)
        failed_materialization_id = resolved_verification_id or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            record_async_run_failure(
                problem,
                user,
                str(target.get('run_id') or ''),
                mode=run_mode,
                source_label=str(target.get('source_label') or ''),
                error=err,
                artifact_verification_id=failed_materialization_id,
                verification_id=verification_id,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                synthesize_failed_tests=False,
                failure_stage='build',
                execution_skipped=True,
            )
        return
    existing_summary = load_verification_summary(config.db, verification_id)
    if not isinstance(existing_summary, dict) or not existing_summary:
        existing_summary = default_verification_summary(
            kind=VERIFICATION_KIND_VERIFICATION,
            mode=run_mode,
            source_paths=[
                str(target.get("source_label") or "").strip()
                for target in targets
                if str(target.get("source_label") or "").strip()
            ],
            verification_source="run.execute",
        )
    else:
        existing_summary = dict(existing_summary)
    existing_summary["artifact_verification_id"] = resolved_verification_id
    existing_summary["artifact_verification_status"] = "ok"
    existing_summary["verification_source"] = "run.execute"
    existing_summary["mode"] = str(run_mode or existing_summary.get("mode") or "pass-fail").strip() or "pass-fail"
    existing_summary["status"] = str(existing_summary.get("status") or "running").strip().lower() or "running"
    create_verification_record(
        config.db,
        config.fs_manager,
        verification_id=verification_id,
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        kind=VERIFICATION_KIND_VERIFICATION,
        status=str(existing_summary.get("status") or "running"),
        summary=existing_summary,
    )
    save_verification_summary(
        config.db,
        verification_id=verification_id,
        status=str(existing_summary.get("status") or "running"),
        summary=existing_summary,
        finished=False,
    )
    _seed_verification_runs(
        problem_id=problem_id,
        workspace_id=workspace_id,
        verification_id=verification_id,
        artifact_verification_id=resolved_verification_id,
        mode=run_mode,
        verification_source="run.execute",
        targets=targets,
        default_task_kind="solve",
    )
    parallelism = _verification_submission_parallelism(len(targets))

    def _prepare_target_submission(target: dict[str, object]) -> tuple[dict[str, object], dict[str, object]] | None:
        run_id = str(target.get('run_id') or '').strip()
        if not run_id:
            return None
        if _run_marked_cancelled(problem_id, workspace_id, run_id):
            return None
        source_label = str(target.get('source_label') or '').strip() or 'upload'
        submission_path_raw = str(target.get('submission_path') or '').strip()
        submission_path_arg = submission_path_raw or None
        upload_filename = str(target.get('upload_filename') or '').strip() or None
        expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
        raw_upload = target.get('upload_content')
        upload_content: bytes | None = None
        if isinstance(raw_upload, bytes):
            upload_content = raw_upload
        elif isinstance(raw_upload, bytearray):
            upload_content = bytes(raw_upload)
        meta: dict[str, object] = {
            'run_id': run_id,
            'source_label': source_label,
            'expected_behavior': expected_behavior,
        }
        submission_kwargs: dict[str, object] = {
            'problem': problem,
            'username': user,
            'artifact_verification_id': resolved_verification_id,
            'submission_path': submission_path_arg,
            'mode': run_mode,
            'upload_content': upload_content,
            'upload_filename': upload_filename,
            'run_id': run_id,
            'verification_id': verification_id,
            'verification_run_ids': verification_run_ids,
            'expected_behavior': expected_behavior,
            'verification_source': 'run.execute',
            'task_kind': 'solve',
        }
        if force_recompile:
            submission_kwargs['force_recompile'] = True
        if selected_test_names:
            submission_kwargs['selected_tests'] = selected_test_names
        return (meta, submission_kwargs)

    def _handle_submission_outcome(meta: dict[str, object], *, returned_run_id: str='', error: Exception | None=None) -> None:
        run_id = str(meta.get('run_id') or '').strip()
        if not run_id:
            return
        source_label = str(meta.get('source_label') or '').strip() or 'upload'
        expected_behavior = normalize_expected_behavior(str(meta.get('expected_behavior') or 'unknown'))
        if error is None:
            annotate_run_id = normalize_run_id_token(returned_run_id) or run_id
            annotate_verification_run_result(
                problem_id,
                workspace_id,
                annotate_run_id,
                verification_id=verification_id,
                expected_behavior=expected_behavior,
                verification_source='run.execute',
            )
            return
        if _run_marked_cancelled(problem_id, workspace_id, run_id):
            return
        record_async_run_failure(
            problem,
            user,
            run_id,
            mode=run_mode,
            source_label=source_label,
            error=str(error),
            artifact_verification_id=resolved_verification_id,
            verification_id=verification_id,
            expected_behavior=expected_behavior,
        )

    if parallelism <= 1:
        for target in targets:
            prepared = _prepare_target_submission(target)
            if prepared is None:
                continue
            meta, submission_kwargs = prepared
            try:
                returned_run_id = str(config.judgehost_task_service.run_submission(**submission_kwargs) or '').strip()
                _handle_submission_outcome(meta, returned_run_id=returned_run_id, error=None)
            except Exception as exc:
                _handle_submission_outcome(meta, error=exc)
    else:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            inflight: dict[object, dict[str, object]] = {}
            next_index = 0

            def _pump_submit() -> None:
                nonlocal next_index
                while next_index < len(targets) and len(inflight) < parallelism:
                    prepared = _prepare_target_submission(targets[next_index])
                    next_index += 1
                    if prepared is None:
                        continue
                    meta, submission_kwargs = prepared
                    future = pool.submit(config.judgehost_task_service.run_submission, **submission_kwargs)
                    inflight[future] = meta

            _pump_submit()
            while inflight:
                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    meta = inflight.pop(future)
                    try:
                        returned_run_id = str(future.result() or '').strip()
                        _handle_submission_outcome(meta, returned_run_id=returned_run_id, error=None)
                    except Exception as exc:
                        _handle_submission_outcome(meta, error=exc)
                _pump_submit()

def start_run_execute_batch(
    problem: str,
    user: str,
    *,
    requested_verification_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    verification_id: str,
    verification_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> bool:
    batch_id = str(verification_id or targets[0].get('run_id') or 'verification').strip() if targets else 'verification'
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_execute_batch_worker(
                problem=problem,
                user=user,
                requested_verification_id=requested_verification_id,
                run_mode=run_mode,
                targets=targets,
                verification_id=verification_id,
                verification_run_ids=verification_run_ids,
                selected_test_names=selected_test_names,
                force_recompile=bool(force_recompile),
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.run_execute_lock:
                    config.run_execute_workers.discard(worker)
    worker, queued, _submit_reason = config.worker_queue_service.submit(
        name=f'run-execute-{batch_id}',
        fn=_runner,
        queue_name='run',
        backend=_BACKEND_NAME,
        job_type='run',
    )
    worker_ref[0] = worker
    if queued:
        with config.run_execute_lock:
            config.run_execute_workers.add(worker)
    return bool(queued)

def _verification_workspace_key(problem_id: int, workspace_id: int) -> str:
    return verification_workspace_key(problem_id, workspace_id)

def _run_verification_start_worker(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, str]],
    verification_id: str,
    verification_signature: str='',
    verification_signature_details: dict[str, str] | None=None,
) -> None:
    planned_run_ids: list[str] = []
    for target in targets:
        token = normalize_run_id_token(target.get('run_id'))
        if not token:
            token = _allocate_run_id()
        target['run_id'] = token
        if token and token not in planned_run_ids:
            planned_run_ids.append(token)
    run_ids: list[str] = list(planned_run_ids)
    artifact_verification_id = _C.RUN_PLACEHOLDER_BUILD_ID
    verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    ws_row = config.db.fetch_one('SELECT path FROM workspaces WHERE id=? AND problem_id=?', [int(workspace_id), int(problem_id)])
    if ws_row is not None:
        try:
            workspace_path = Path(str(ws_row['path'] or '')).resolve()
            _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
            verification_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        except Exception:
            verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    verification_details: dict[str, object] = {'status': 'failed', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'submission_paths': [str(item.get('path') or '') for item in targets], 'solution_count': len(targets), 'verification_id': verification_id, 'verification_backend': _BACKEND_NAME, 'error': ''}
    verification_details['mode'] = verification_mode
    verification_details['source_paths'] = [str(item.get('path') or '').strip() for item in targets if str(item.get('path') or '').strip()]
    initial_runs: dict[str, dict[str, object]] = {}
    initial_runs_order: list[str] = []
    for target in targets:
        run_id = normalize_run_id_token(target.get('run_id'))
        if not run_id:
            continue
        source_path = (
            str(target.get('path') or '').strip()
            or str(target.get('source_label') or '').strip()
            or run_id
        )
        expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
        run_root = config.fs_manager.prepare_verification_run_root(verification_id, run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        run_row = default_verification_run(
            run_id=run_id,
            source_label=source_path,
            expected_behavior=expected_behavior,
            run_status='queued',
            artifact_path=str(run_root),
            task_kind='solve',
        )
        run_row['summary'] = {
            'artifact_verification_id': _C.RUN_PLACEHOLDER_BUILD_ID,
            'mode': verification_mode,
            'source': source_path,
            'tests': [],
            'compile_diagnostics': [],
            'error': '',
            'verification_source': 'verification.start',
        }
        initial_runs[run_id] = run_row
        if run_id not in initial_runs_order:
            initial_runs_order.append(run_id)
    verification_details['runs'] = initial_runs
    verification_details['runs_order'] = initial_runs_order
    if verification_signature:
        verification_details['verification_signature'] = verification_signature
    if isinstance(verification_signature_details, dict) and verification_signature_details:
        verification_details['verification_signature_details'] = dict(verification_signature_details)

    def _persist_verification_details(*, finished: bool=False) -> None:
        if not verification_id:
            return
        status_token = str(verification_details.get('status') or 'running').strip().lower()
        table_status = 'running'
        if status_token in {'pass', 'ok', 'passed'}:
            table_status = 'ok'
        elif status_token not in {'', 'queued', 'pending', 'running'}:
            table_status = 'failed'
        summary_payload = dict(verification_details)
        summary_payload['verification_id'] = verification_id
        summary_payload['updated_at'] = now_iso()
        try:
            with verification_update_lock(verification_id):
                existing_summary = load_verification_summary(config.db, verification_id)
                if not isinstance(existing_summary, dict):
                    existing_summary = {}
                merged_summary = dict(existing_summary)
                merged_summary.update(summary_payload)
                create_verification_record(
                    config.db,
                    config.fs_manager,
                    verification_id=verification_id,
                    problem_id=int(problem_id),
                    workspace_id=int(workspace_id),
                    source_commit=str(workspace_head or "").strip(),
                    source_ref=str(workspace_head or "").strip(),
                    kind=VERIFICATION_KIND_VERIFICATION,
                    status=table_status,
                    summary=merged_summary,
                )
                save_verification_summary(
                    config.db,
                    verification_id=verification_id,
                    status=table_status,
                    summary=merged_summary,
                    finished=bool(finished),
                )
        except Exception:
            pass

    verification_details['status'] = 'running'
    _persist_verification_details(finished=False)
    verification_details.pop('runs', None)
    verification_details.pop('runs_order', None)

    def _backfill_missing_verification_runs(
        error_text: str,
        *,
        build_for_failure: str,
        execution_skipped_for_missing: bool=False,
        synthesized_test_names: list[str] | None = None,
    ) -> None:
        safe_error = str(error_text or '').strip() or 'verification failed'
        safe_build_for_failure = str(build_for_failure or _C.RUN_PLACEHOLDER_BUILD_ID).strip() or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            token = normalize_run_id_token(target.get('run_id'))
            if not token:
                continue
            if token not in run_ids:
                run_ids.append(token)
            existing = load_verification_run(
                config.db,
                verification_id=verification_id,
                run_id=token,
            )
            if isinstance(existing, dict) and existing:
                continue
            record_async_run_failure(
                problem,
                user,
                token,
                mode=verification_mode,
                source_label=str(target.get('path') or ''),
                error=safe_error,
                artifact_verification_id=safe_build_for_failure,
                verification_id=verification_id,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                verification_source='verification.start',
                synthesize_failed_tests=bool(synthesized_test_names) or (not bool(execution_skipped_for_missing)),
                failure_stage='build' if execution_skipped_for_missing else '',
                execution_skipped=bool(execution_skipped_for_missing),
                synthesized_test_names=list(synthesized_test_names or []),
            )
        deduped = dedupe_preserve_order(run_ids)
        run_ids.clear()
        run_ids.extend(deduped)

    buildsolve_row: dict[str, object] | None = None
    buildsolve_summary: dict | None = None
    accepted_source_path = ''
    try:
        if ws_row is None:
            raise RuntimeError('workspace metadata missing')
        workspace_path = Path(str(ws_row['path'] or '')).resolve()
        if (not workspace_path.exists()) or (not workspace_path.is_dir()) or workspace_path.is_symlink():
            raise RuntimeError('workspace path is unavailable')
        local_ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
        try:
            artifact_verification_id, _implicit_created = _ensure_implicit_verification(
                problem,
                user,
                ctx=local_ctx,
                for_verification=True,
                verification_id=verification_id,
            )
        except VerificationFailureError as build_exc:
            artifact_verification_id = str(build_exc.verification_id or '').strip() or _C.RUN_PLACEHOLDER_BUILD_ID
            verification_details['artifact_verification_id'] = artifact_verification_id
            verification_details['artifact_verification_status'] = str(build_exc.status or 'failed').strip().lower() or 'failed'
            verification_details['source_commit'] = str(workspace_head or '').strip()
            verification_details['source_ref'] = str(workspace_head or '').strip()
            verification_details['artifact_verification_error'] = str(build_exc.reason or str(build_exc)).strip()
            verification_details['artifact_failed_step'] = ''
            verification_details['artifact_failed_test'] = str(build_exc.failed_test or '').strip()
            _merge_verification_stage_context(
                problem_id=problem_id,
                workspace_id=workspace_id,
                artifact_verification_id=artifact_verification_id,
                verification_details=verification_details,
            )
            try:
                solution_entries, _truncated = list_solution_entries(workspace_path)
                accepted_source_path = str(
                    resolve_build_accepted_solution_source(workspace_path, solution_entries)
                ).strip()
            except Exception:
                accepted_source_path = ''
            buildsolve_row, buildsolve_summary = _find_solve_main_run(
                problem_id,
                workspace_id,
                artifact_verification_id,
                accepted_source_path,
            )
            if (not accepted_source_path) and isinstance(buildsolve_summary, dict):
                accepted_source_path = str(buildsolve_summary.get('source') or '').strip()
            known_failed_tests: list[str] = []
            if isinstance(buildsolve_summary, dict):
                for item in buildsolve_summary.get('tests') or []:
                    if not isinstance(item, dict):
                        continue
                    test_name = str(item.get('test') or '').strip()
                    if test_name and test_name not in known_failed_tests:
                        known_failed_tests.append(test_name)
            stage_results_obj = verification_details.get('stage_results')
            stage_results = stage_results_obj if isinstance(stage_results_obj, dict) else {}
            if not known_failed_tests:
                generate_stage = stage_results.get('generate_input')
                if isinstance(generate_stage, dict):
                    for item in generate_stage.get('tests') or []:
                        if not isinstance(item, dict):
                            continue
                        test_name = str(item.get('test') or '').strip()
                        if test_name and test_name not in known_failed_tests:
                            known_failed_tests.append(test_name)
            for target in targets:
                source_path = str(target.get('path') or '').strip()
                expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
                if (
                    accepted_source_path
                    and source_path == accepted_source_path
                    and expected_behavior == 'accepted'
                    and buildsolve_row is not None
                    and isinstance(buildsolve_summary, dict)
                ):
                    materialized_run_id = normalize_run_id_token(target.get('run_id')) or _allocate_run_id()
                    target['run_id'] = materialized_run_id
                    if materialized_run_id not in run_ids:
                        run_ids.append(materialized_run_id)
                    _materialize_reused_buildsolve_run(
                        problem_id=problem_id,
                        workspace_id=workspace_id,
                        run_id=materialized_run_id,
                        mode=verification_mode,
                        buildsolve_row=buildsolve_row,
                        buildsolve_summary=dict(buildsolve_summary),
                        verification_id=verification_id,
                        verification_run_ids=run_ids,
                        expected_behavior=expected_behavior,
                    )
            _backfill_missing_verification_runs(
                str(build_exc),
                build_for_failure=artifact_verification_id,
                execution_skipped_for_missing=True,
                synthesized_test_names=known_failed_tests,
            )
            verification_details['status'] = 'failed'
            verification_details['error'] = str(build_exc)
            _persist_verification_details(finished=True)
            audit(actor_user_id, problem_id, 'verification.start', verification_details)
            return
        verification_details['artifact_verification_id'] = artifact_verification_id
        verification_details['artifact_verification_status'] = 'ok'
        verification_details['source_commit'] = str(workspace_head or '').strip()
        verification_details['source_ref'] = str(workspace_head or '').strip()
        verification_details['artifact_verification_error'] = ''
        verification_details['artifact_failed_step'] = ''
        verification_details['artifact_failed_test'] = ''
        _merge_verification_stage_context(
            problem_id=problem_id,
            workspace_id=workspace_id,
            artifact_verification_id=artifact_verification_id,
            verification_details=verification_details,
        )
        selected_test_names = _verification_generated_test_names(verification_details)
        _persist_verification_details(finished=False)
        try:
            solution_entries, _truncated = list_solution_entries(workspace_path)
            accepted_source_path = str(
                resolve_build_accepted_solution_source(workspace_path, solution_entries)
            ).strip()
        except Exception:
            accepted_source_path = ''
        buildsolve_row, buildsolve_summary = _find_solve_main_run(
            problem_id,
            workspace_id,
            artifact_verification_id,
            accepted_source_path,
        )
        if (not accepted_source_path) and isinstance(buildsolve_summary, dict):
            accepted_source_path = str(buildsolve_summary.get('source') or '').strip()
        target_specs: list[dict[str, object]] = []
        for index, target in enumerate(targets):
            source_path = str(target.get('path') or '').strip()
            expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
            requested_run_id = normalize_run_id_token(target.get('run_id'))
            if not requested_run_id:
                requested_run_id = _allocate_run_id()
                target['run_id'] = requested_run_id
            reuse_buildsolve = bool(
                accepted_source_path
                and buildsolve_row is not None
                and buildsolve_summary is not None
                and expected_behavior == 'accepted'
                and source_path == accepted_source_path
            )
            target_specs.append(
                {
                    'index': int(index),
                    'target': target,
                    'source_path': source_path,
                    'expected_behavior': expected_behavior,
                    'requested_run_id': requested_run_id,
                    'reuse_buildsolve': reuse_buildsolve,
                }
            )
        for spec in target_specs:
            requested_run_id = normalize_run_id_token(spec.get('requested_run_id'))
            if not requested_run_id:
                continue
            target_ref = spec.get('target')
            if isinstance(target_ref, dict):
                target_ref['run_id'] = requested_run_id
            if requested_run_id not in run_ids:
                run_ids.append(requested_run_id)
        run_ids = dedupe_preserve_order(run_ids)
        _seed_verification_runs(
            problem_id=problem_id,
            workspace_id=workspace_id,
            verification_id=verification_id,
            artifact_verification_id=artifact_verification_id,
            mode=verification_mode,
            verification_source='verification.start',
            targets=[
                {
                    'run_id': str(spec.get('requested_run_id') or ''),
                    'path': str(spec.get('source_path') or ''),
                    'expected_behavior': str(spec.get('expected_behavior') or 'unknown'),
                    'task_kind': 'solve',
                }
                for spec in target_specs
            ],
            default_task_kind='solve',
        )
        for spec in target_specs:
            if not bool(spec.get('reuse_buildsolve')):
                continue
            if buildsolve_row is None or not isinstance(buildsolve_summary, dict):
                raise RuntimeError('verification.solve-main result missing for accepted solution reuse')
            requested_run_id = normalize_run_id_token(spec.get('requested_run_id'))
            expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
            if not requested_run_id:
                continue
            _materialize_reused_buildsolve_run(
                problem_id=problem_id,
                workspace_id=workspace_id,
                run_id=requested_run_id,
                mode=verification_mode,
                buildsolve_row=buildsolve_row,
                buildsolve_summary=dict(buildsolve_summary),
                verification_id=verification_id,
                verification_run_ids=run_ids,
                expected_behavior=expected_behavior,
            )
        solution_results_by_index: dict[int, dict[str, object]] = {}
        cancel_reason = 'verification cancelled by user'
        cancel_requested = False

        def _verification_cancel_requested() -> bool:
            nonlocal cancel_requested
            if cancel_requested:
                return True
            if _verification_marked_cancelled(problem_id, actor_user_id, verification_id):
                cancel_requested = True
            return cancel_requested

        parallelism = _verification_submission_parallelism(len(target_specs))
        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            inflight: dict[object, dict[str, object]] = {}
            next_index = 0

            def _store_target_result(
                spec: dict[str, object],
                *,
                current_run_id: str,
                run_row: dict[str, object] | None,
                summary_obj: dict | None,
            ) -> None:
                source_path = str(spec.get('source_path') or '').strip()
                expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                run_status = str(run_row['status'] or 'missing').strip().lower() if run_row is not None else 'missing'
                if run_status in {'queued', 'pending'} and (not isinstance(summary_obj, dict)):
                    summary_obj = {}
                matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, run_status, summary_obj)
                error_text = str(summary_obj.get('error') or '') if isinstance(summary_obj, dict) else ''
                spec_index = int(spec.get('index') or 0)
                solution_results_by_index[spec_index] = {
                    'source_path': source_path,
                    'expected_behavior': expected_behavior,
                    'run_id': current_run_id,
                    'run_status': run_status,
                    'completed': completed,
                    'passed_all_tests': observed_pass,
                    'matched': matched,
                    'reason': reason,
                    'error': error_text,
                }

            def _pump_submit() -> None:
                nonlocal next_index, run_ids
                while next_index < len(target_specs) and len(inflight) < max(1, parallelism):
                    spec = target_specs[next_index]
                    next_index += 1
                    source_path = str(spec.get('source_path') or '').strip()
                    expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                    requested_run_id = normalize_run_id_token(spec.get('requested_run_id'))
                    target_ref = spec.get('target')
                    if _verification_cancel_requested():
                        cancel_run_id = requested_run_id or _allocate_run_id()
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = cancel_run_id
                        if cancel_run_id not in run_ids:
                            run_ids.append(cancel_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        run_row, summary_obj = _load_execution_result(
                            problem_id=problem_id,
                            workspace_id=workspace_id,
                            verification_id=verification_id,
                            run_id=cancel_run_id,
                        )
                        if not _run_marked_cancelled(problem_id, workspace_id, cancel_run_id):
                            record_async_run_failure(
                                problem,
                                user,
                                cancel_run_id,
                                mode=verification_mode,
                                source_label=source_path,
                                error=cancel_reason,
                                artifact_verification_id=artifact_verification_id,
                                verification_id=verification_id,
                                expected_behavior=expected_behavior,
                                verification_source='verification.start',
                                synthesize_failed_tests=False,
                                failure_stage='cancel',
                                execution_skipped=True,
                            )
                            run_row, summary_obj = _load_execution_result(
                                problem_id=problem_id,
                                workspace_id=workspace_id,
                                verification_id=verification_id,
                                run_id=cancel_run_id,
                            )
                        _store_target_result(spec, current_run_id=cancel_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    if requested_run_id and _run_marked_cancelled(problem_id, workspace_id, requested_run_id):
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = requested_run_id
                        if requested_run_id not in run_ids:
                            run_ids.append(requested_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        run_row, summary_obj = _load_execution_result(
                            problem_id=problem_id,
                            workspace_id=workspace_id,
                            verification_id=verification_id,
                            run_id=requested_run_id,
                        )
                        _store_target_result(spec, current_run_id=requested_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    if bool(spec.get('reuse_buildsolve')):
                        if buildsolve_row is None or not isinstance(buildsolve_summary, dict):
                            raise RuntimeError('verification.solve-main result missing for accepted solution reuse')
                        submission_run_id = requested_run_id or _allocate_run_id()
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = submission_run_id
                        if submission_run_id and submission_run_id not in run_ids:
                            run_ids.append(submission_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        run_row, summary_obj = _materialize_reused_buildsolve_run(
                            problem_id=problem_id,
                            workspace_id=workspace_id,
                            run_id=submission_run_id,
                            mode=verification_mode,
                            buildsolve_row=buildsolve_row,
                            buildsolve_summary=dict(buildsolve_summary),
                            verification_id=verification_id,
                            verification_run_ids=run_ids,
                            expected_behavior=expected_behavior,
                        )
                        _store_target_result(
                            spec,
                            current_run_id=submission_run_id,
                            run_row=run_row,
                            summary_obj=summary_obj,
                        )
                        continue
                    submission_run_id = requested_run_id or _allocate_run_id()
                    if isinstance(target_ref, dict):
                        target_ref['run_id'] = submission_run_id
                    if submission_run_id and submission_run_id not in run_ids:
                        run_ids.append(submission_run_id)
                    run_ids = dedupe_preserve_order(run_ids)
                    verification_run_ids_snapshot = list(run_ids)
                    future = pool.submit(
                        config.judgehost_task_service.run_submission,
                        problem=problem,
                        username=user,
                        artifact_verification_id=artifact_verification_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=submission_run_id,
                        verification_id=verification_id,
                        verification_run_ids=verification_run_ids_snapshot,
                        expected_behavior=expected_behavior,
                        verification_source='verification.start',
                        task_kind='solve',
                        selected_tests=selected_test_names,
                    )
                    inflight[future] = spec

            _pump_submit()
            while inflight:
                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    spec = inflight.pop(future)
                    source_path = str(spec.get('source_path') or '').strip()
                    expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                    requested_run_id = normalize_run_id_token(spec.get('requested_run_id'))
                    target_ref = spec.get('target')
                    current_run_id = requested_run_id or ''
                    run_row = None
                    summary_obj: dict | None = None
                    try:
                        current_run_id = str(future.result() or '').strip()
                        current_run_id = normalize_run_id_token(current_run_id) or current_run_id
                        if requested_run_id and current_run_id and requested_run_id != current_run_id:
                            run_ids = [current_run_id if token == requested_run_id else token for token in run_ids]
                        if current_run_id and current_run_id not in run_ids:
                            run_ids.append(current_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = current_run_id
                        run_row, summary_obj = _load_execution_result(
                            problem_id=problem_id,
                            workspace_id=workspace_id,
                            verification_id=verification_id,
                            run_id=current_run_id,
                        )
                        if run_row is None:
                            raise RuntimeError('run metadata missing after submission')
                    except Exception as target_exc:
                        fallback_run_id = normalize_run_id_token(current_run_id) or requested_run_id
                        if fallback_run_id:
                            current_run_id = fallback_run_id
                            if isinstance(target_ref, dict):
                                target_ref['run_id'] = fallback_run_id
                            if fallback_run_id not in run_ids:
                                run_ids.append(fallback_run_id)
                            run_ids = dedupe_preserve_order(run_ids)
                            if not _run_marked_cancelled(problem_id, workspace_id, fallback_run_id):
                                record_async_run_failure(
                                    problem,
                                    user,
                                    fallback_run_id,
                                    mode=verification_mode,
                                    source_label=source_path,
                                    error=str(target_exc),
                                    artifact_verification_id=artifact_verification_id,
                                    verification_id=verification_id,
                                    expected_behavior=expected_behavior,
                                    verification_source='verification.start',
                                )
                            run_row, summary_obj = _load_execution_result(
                                problem_id=problem_id,
                                workspace_id=workspace_id,
                                verification_id=verification_id,
                                run_id=fallback_run_id,
                            )
                        else:
                            summary_obj = {'error': str(target_exc)}
                    _store_target_result(spec, current_run_id=current_run_id, run_row=run_row, summary_obj=summary_obj)
                _pump_submit()

        for target in targets:
            token = normalize_run_id_token(target.get('run_id'))
            if token and token not in run_ids:
                run_ids.append(token)
        run_ids = dedupe_preserve_order(run_ids)

        # Retry accepted-solution mismatches once in serial to reduce transient
        # timing flakiness under heavy parallel judgehost load.
        retry_specs: list[dict[str, object]] = []
        for spec in target_specs:
            spec_index = int(spec.get('index') or 0)
            item = solution_results_by_index.get(spec_index)
            if not isinstance(item, dict):
                continue
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or spec.get('expected_behavior') or 'unknown'))
            if expected_behavior != 'accepted':
                continue
            if bool(spec.get('reuse_buildsolve')):
                continue
            if bool(item.get('matched')):
                continue
            run_status = str(item.get('run_status') or '').strip().lower()
            if run_status in {'running', 'queued', 'pending'}:
                continue
            retry_specs.append(spec)

        for spec in retry_specs:
            if _verification_cancel_requested():
                break
            spec_index = int(spec.get('index') or 0)
            previous_item = solution_results_by_index.get(spec_index)
            previous_run_id = ''
            if isinstance(previous_item, dict):
                previous_run_id = normalize_run_id_token(previous_item.get('run_id'))
            source_path = str(spec.get('source_path') or '').strip()
            target_ref = spec.get('target')
            retry_run_id = _allocate_run_id()
            try:
                submitted_run_id = str(
                    config.judgehost_task_service.run_submission(
                        problem=problem,
                        username=user,
                        artifact_verification_id=artifact_verification_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=retry_run_id,
                        verification_id=verification_id,
                        verification_run_ids=list(run_ids),
                        expected_behavior='accepted',
                        verification_source='verification.start',
                        task_kind='solve',
                        selected_tests=selected_test_names,
                    )
                    or ''
                ).strip()
                normalized_submitted = normalize_run_id_token(submitted_run_id)
                if normalized_submitted:
                    retry_run_id = normalized_submitted
                run_row, summary_obj = _load_execution_result(
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    verification_id=verification_id,
                    run_id=retry_run_id,
                )
                if run_row is None:
                    continue
                _store_target_result(spec, current_run_id=retry_run_id, run_row=run_row, summary_obj=summary_obj)
                if isinstance(target_ref, dict):
                    target_ref['run_id'] = retry_run_id
                replaced = False
                rewritten_run_ids: list[str] = []
                for token in run_ids:
                    if (not replaced) and previous_run_id and token == previous_run_id:
                        rewritten_run_ids.append(retry_run_id)
                        replaced = True
                    else:
                        rewritten_run_ids.append(token)
                if (not replaced) and retry_run_id:
                    rewritten_run_ids.append(retry_run_id)
                run_ids = dedupe_preserve_order(rewritten_run_ids)
            except Exception:
                # Keep original mismatch result when retry itself fails.
                continue

        solution_results: list[dict[str, object]] = []
        first_reason = ''
        for spec in target_specs:
            spec_index = int(spec.get('index') or 0)
            item = solution_results_by_index.get(spec_index)
            if not isinstance(item, dict):
                continue
            if bool(item.get('completed')) and (not bool(item.get('matched'))) and (not first_reason):
                first_reason = _verification_solution_failure_hint(
                    str(item.get('source_path') or ''),
                    str(item.get('reason') or ''),
                    str(item.get('error') or ''),
                )
            solution_results.append(item)
        verification_details['solutions'] = solution_results
        all_completed = bool(solution_results) and all((bool(item.get('completed')) for item in solution_results))
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
        if all_completed:
            verification_details['status'] = 'pass' if passed else 'failed'
            if not passed and first_reason:
                verification_details['error'] = first_reason
        else:
            verification_details['status'] = 'running'
            verification_details['error'] = ''
        _persist_verification_details(finished=False)
        for item in solution_results:
            current_run_id = str(item.get('run_id') or '').strip()
            if not current_run_id:
                continue
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            annotated = annotate_verification_run_result(
                problem_id,
                workspace_id,
                current_run_id,
                verification_id=verification_id,
                expected_behavior=expected_behavior,
                verification_source='verification.start',
            )
            item['matched'] = bool(annotated.get('matched'))
            item['completed'] = bool(annotated.get('completed'))
            item['passed_all_tests'] = bool(annotated.get('passed_all_tests'))
            item['reason'] = str(annotated.get('reason') or item.get('reason') or '')
        all_completed = bool(solution_results) and all((bool(item.get('completed')) for item in solution_results))
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
        if all_completed:
            verification_details['status'] = 'pass' if passed else 'failed'
            if not passed:
                unmatched = next((item for item in solution_results if (isinstance(item, dict) and (not bool(item.get('matched'))))), None)
                if isinstance(unmatched, dict):
                    reason_first = _verification_solution_failure_hint(
                        str(unmatched.get('source_path') or ''),
                        str(unmatched.get('reason') or ''),
                        str(unmatched.get('error') or ''),
                    )
                    if reason_first:
                        verification_details['error'] = reason_first
            _persist_verification_details(finished=True)
        else:
            verification_details['status'] = 'running'
            verification_details['error'] = ''
            _persist_verification_details(finished=False)
    except Exception as exc:
        safe_artifact_verification_status = str(
            verification_details.get('artifact_verification_status')
            or ''
        ).strip().lower()
        materialization_stage_failed = safe_artifact_verification_status in {'failed', 'error', 'missing'} or str(exc).strip().lower().startswith('verification failed')
        _backfill_missing_verification_runs(
            str(exc),
            build_for_failure=artifact_verification_id,
            execution_skipped_for_missing=materialization_stage_failed,
        )
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
        _persist_verification_details(finished=True)
    audit(actor_user_id, problem_id, 'verification.start', verification_details)

def start_verification_job(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, str]],
    verification_id: str,
    initial_details: dict[str, object] | None=None,
    workspace_path: Path | str | None=None,
) -> bool:
    key = _verification_workspace_key(problem_id, workspace_id)
    verification_signature = ''
    verification_signature_details: dict[str, str] = {}
    if workspace_path:
        try:
            workspace_obj = Path(str(workspace_path))
            verification_signature = _verification_sources_signature(workspace_obj)
            verification_signature_details = _verification_sources_signature_details(workspace_obj)
        except Exception:
            verification_signature = ''
            verification_signature_details = {}
    if isinstance(initial_details, dict) and verification_signature and (not str(initial_details.get('verification_signature') or '').strip()):
        initial_details['verification_signature'] = verification_signature
    if isinstance(initial_details, dict) and verification_signature_details and (not isinstance(initial_details.get('verification_signature_details'), dict)):
        initial_details['verification_signature_details'] = dict(verification_signature_details)
    with config.verification_lock:
        if key in config.verification_inflight:
            return False
        config.verification_inflight.add(key)
    if isinstance(initial_details, dict):
        try:
            audit(actor_user_id, problem_id, 'verification.start', initial_details)
        except Exception:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_verification_start_worker(
                problem,
                user,
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
                targets=targets,
                verification_id=verification_id,
                verification_signature=verification_signature,
                verification_signature_details=verification_signature_details,
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.verification_lock:
                    config.verification_workers.discard(worker)
                    config.verification_inflight.discard(key)
    thread_name = verification_id if verification_id else key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            backend=_BACKEND_NAME,
            dedupe_key=f'verification:{key}',
            job_type='verification',
        )
        worker_ref[0] = worker
        if not queued:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'verification queue rejected ({submit_reason})')
        with config.verification_lock:
            config.verification_workers.add(worker)
    except Exception:
        with config.verification_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.verification_workers.discard(worker)
            config.verification_inflight.discard(key)
        raise
    return True

# Referenced via dynamic re-export in workspace.api/public.
_DYNAMIC_EXPORT_KEEP = (start_verification_job,)
_ = len(_DYNAMIC_EXPORT_KEEP)

def _export_workspace_key(problem_id: int, workspace_id: int, head_commit: str, export_type: str) -> str:
    return export_workspace_key(problem_id, workspace_id, head_commit, export_type)

def _run_export_create_worker(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_verification_id: str, requested_export_type: str) -> None:
    safe_requested_verification_id = str(requested_verification_id or '').strip()
    safe_export_type = str(requested_export_type or '').strip().lower() or 'icpc'
    details: dict[str, object] = {'status': 'failed', 'artifact_verification_id': safe_requested_verification_id, 'export_type': safe_export_type, 'source_commit': str(head_commit or '').strip(), 'filename': '', 'error': ''}
    worker_error: Exception | None = None
    try:
        if safe_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        safe_head = str(head_commit or '').strip()
        if not safe_head:
            raise ValueError('no committed revision; commit changes first')
        resolved_verification_id = safe_requested_verification_id
        if not resolved_verification_id:
            active_verification = latest_workspace_committed_stage_verification(int(problem_id), int(workspace_id), safe_head, ok_only=True)
            if active_verification is None:
                resolved_verification_id = config.verification_service.run_verification(problem, user, commit=safe_head, ref=safe_head)
            else:
                resolved_verification_id = str(active_verification['id'] or '').strip()
        if not resolved_verification_id:
            raise RuntimeError('failed to resolve verification for export')
        materialization_row = config.db.fetch_one(
            "SELECT status,source_commit,source_ref FROM verifications WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'",
            [resolved_verification_id, int(problem_id), int(workspace_id)],
        )
        if materialization_row is None:
            raise ValueError(f'verification not found: {resolved_verification_id}')
        artifact_verification_status = str(materialization_row['status'] or 'missing').strip().lower()
        source_commit = str(materialization_row['source_commit'] or '').strip()
        source_ref = str(materialization_row['source_ref'] or '').strip()
        details['artifact_verification_id'] = resolved_verification_id
        details['artifact_verification_status'] = artifact_verification_status
        details['source_commit'] = source_commit
        details['source_ref'] = source_ref
        if source_commit != safe_head or source_ref != safe_head:
            raise ValueError('package must be generated from current committed revision')
        if artifact_verification_status != 'ok':
            raise ValueError(f'verification status is {artifact_verification_status}')
        out = config.export_service.create_export(problem, resolved_verification_id, safe_export_type)
        details['status'] = 'ok'
        details['filename'] = out.name
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        worker_error = exc
    audit(actor_user_id, problem_id, 'export.create', details)
    if worker_error is not None:
        raise worker_error

def start_export_job(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_verification_id: str, requested_export_type: str, initial_details: dict[str, object] | None=None) -> bool:
    key = _export_workspace_key(problem_id, workspace_id, head_commit, requested_export_type)
    with config.export_lock:
        if key in config.export_inflight:
            return False
        config.export_inflight.add(key)
    if isinstance(initial_details, dict):
        try:
            audit(actor_user_id, problem_id, 'export.create', initial_details)
        except Exception:
            with config.export_lock:
                config.export_inflight.discard(key)
            raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_export_create_worker(problem, user, actor_user_id=actor_user_id, problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_verification_id=requested_verification_id, requested_export_type=requested_export_type)
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.export_lock:
                    config.export_workers.discard(worker)
                    config.export_inflight.discard(key)
    thread_name = key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            backend=_BACKEND_NAME,
            dedupe_key=f'export:{key}',
            job_type='export',
        )
        worker_ref[0] = worker
        if not queued:
            with config.export_lock:
                config.export_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'export queue rejected ({submit_reason})')
        with config.export_lock:
            config.export_workers.add(worker)
    except Exception:
        with config.export_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.export_workers.discard(worker)
            config.export_inflight.discard(key)
        raise
    return True
