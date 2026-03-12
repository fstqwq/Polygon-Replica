from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import uuid

from app.db import now_iso
from app.impl.runtime.config import config

from app.service.problem.solution_metadata import normalize_expected_behavior

from .artifact import assert_workspace_build_access
from .context_operation import audit, dedupe_preserve_order, parse_summary_json
from .context_operation import list_solution_entries, resolve_build_accepted_solution_source
from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx
from .context_verification import (
    latest_workspace_committed_build,
    _verification_solution_failure_hint,
    _verification_solution_match,
    _verification_sources_signature,
    _verification_sources_signature_details,
)
from .export_dispatch import export_workspace_key
from .problem_config import normalize_problem_mode, read_problem_config
from .verification_dispatch import verification_workspace_key
from .context_job_helper import (
    _allocate_run_id,
    _annotate_run_invocation_result,
    _ensure_implicit_build,
    record_async_run_failure,
)

_C = config.constants


def _run_marked_cancelled(problem_id: int, workspace_id: int, run_id: str) -> bool:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return False
    row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [safe_run_id, int(problem_id), int(workspace_id)])
    if row is None:
        return False
    status = str(row['status'] or '').strip().lower()
    if status != 'failed':
        return False
    summary_obj = parse_summary_json(row['summary_json'], f'cancel/{safe_run_id}')
    if not isinstance(summary_obj, dict):
        return False
    if bool(summary_obj.get('cancelled')):
        return True
    error_text = str(summary_obj.get('error') or '').strip().lower()
    return 'cancelled by user' in error_text

def _invocation_marked_cancelled(problem_id: int, actor_user_id: int, invocation_id: str, *, limit: int = 240) -> bool:
    safe_invocation_id = normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
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
        if normalize_run_id_token(details.get('invocation_id')) == safe_invocation_id:
            return True
    return False

def _invocation_submission_parallelism(target_count: int) -> int:
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


def _find_buildsolve_run(
    problem_id: int,
    workspace_id: int,
    build_id: str,
    accepted_source_path: str,
) -> tuple[dict[str, object] | None, dict | None]:
    safe_build_id = str(build_id or "").strip()
    safe_source = str(accepted_source_path or "").strip()
    if not safe_build_id:
        return (None, None)
    rows = config.db.fetch_all(
        """
        SELECT id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at
        FROM runs
        WHERE problem_id=? AND workspace_id=? AND build_id=?
        ORDER BY created_at DESC
        LIMIT 256
        """,
        [int(problem_id), int(workspace_id), safe_build_id],
    )
    for row in rows:
        row_dict = dict(row)
        summary_obj = parse_summary_json(row_dict.get("summary_json"), f"buildsolve/{row_dict.get('id')}")
        if not isinstance(summary_obj, dict):
            continue
        invocation_block = summary_obj.get("invocation")
        invocation_source = str(invocation_block.get("source") or "").strip().lower() if isinstance(invocation_block, dict) else ""
        if invocation_source != "build.solve":
            continue
        source_path = str(summary_obj.get("source") or "").strip()
        if safe_source and source_path and source_path != safe_source:
            continue
        return (row_dict, summary_obj)
    return (None, None)


def _materialize_reused_buildsolve_run(
    *,
    problem_id: int,
    workspace_id: int,
    run_id: str,
    mode: str,
    buildsolve_row: dict[str, object],
    buildsolve_summary: dict,
    invocation_id: str,
    invocation_run_ids: list[str],
    expected_behavior: str,
) -> tuple[dict[str, object] | None, dict | None]:
    safe_run_id = normalize_run_id_token(run_id)
    if not safe_run_id:
        return (None, None)
    safe_mode = str(mode or "").strip() or "pass-fail"
    now_text = now_iso()
    encoded_summary = json.dumps(buildsolve_summary)
    finished_at = str(buildsolve_row.get("finished_at") or "").strip()
    status = str(buildsolve_row.get("status") or "running").strip() or "running"
    existing = config.db.fetch_one(
        "SELECT id FROM runs WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_run_id, int(problem_id), int(workspace_id)],
    )
    params = [
        str(buildsolve_row.get("build_id") or ""),
        str(buildsolve_row.get("build_ref") or ""),
        safe_mode,
        status,
        encoded_summary,
        str(buildsolve_row.get("artifact_path") or ""),
    ]
    if existing is None:
        config.db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                safe_run_id,
                int(problem_id),
                int(workspace_id),
                *params,
                now_text,
                finished_at or now_text,
            ],
        )
    else:
        config.db.execute(
            """
            UPDATE runs
            SET build_id=?,build_ref=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=?
            WHERE id=? AND problem_id=? AND workspace_id=?
            """,
            [
                *params,
                finished_at or now_text,
                safe_run_id,
                int(problem_id),
                int(workspace_id),
            ],
        )
    _annotate_run_invocation_result(
        problem_id,
        workspace_id,
        safe_run_id,
        invocation_id=invocation_id,
        invocation_run_ids=invocation_run_ids,
        expected_behavior=expected_behavior,
        invocation_source="verification.start",
    )
    row = config.db.fetch_one(
        "SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_run_id, int(problem_id), int(workspace_id)],
    )
    summary_obj = parse_summary_json(row["summary_json"] if row is not None else None, f"verification/{safe_run_id}")
    return (dict(row) if row is not None else None, summary_obj if isinstance(summary_obj, dict) else {})

def _run_execute_batch_worker(
    problem: str,
    user: str,
    *,
    requested_build_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    invocation_id: str,
    invocation_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> None:
    resolved_build_id = str(requested_build_id or '').strip()
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
        problem_id = int(ctx['problem']['id'])
        workspace_id = int(ctx['workspace']['id'])
        if not resolved_build_id:
            resolved_build_id, _ = _ensure_implicit_build(problem, user, ctx=ctx, force=False)
        if not resolved_build_id:
            raise RuntimeError('tests generation did not produce a runnable build')
        assert_workspace_build_access(ctx, resolved_build_id)
    except Exception as exc:
        err = str(exc)
        failed_build_id = resolved_build_id or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            record_async_run_failure(
                problem,
                user,
                str(target.get('run_id') or ''),
                mode=run_mode,
                source_label=str(target.get('source_label') or ''),
                error=err,
                build_id=failed_build_id,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                synthesize_failed_tests=False,
                failure_stage='build',
                execution_skipped=True,
            )
        return
    parallelism = _invocation_submission_parallelism(len(targets))

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
            'build_id': resolved_build_id,
            'submission_path': submission_path_arg,
            'mode': run_mode,
            'upload_content': upload_content,
            'upload_filename': upload_filename,
            'run_id': run_id,
            'invocation_id': invocation_id,
            'invocation_run_ids': invocation_run_ids,
            'expected_behavior': expected_behavior,
            'invocation_source': 'run.execute',
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
            _annotate_run_invocation_result(
                problem_id,
                workspace_id,
                annotate_run_id,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior=expected_behavior,
                invocation_source='run.execute',
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
            build_id=resolved_build_id,
            invocation_id=invocation_id,
            invocation_run_ids=invocation_run_ids,
            expected_behavior=expected_behavior,
        )

    if parallelism <= 1:
        for target in targets:
            prepared = _prepare_target_submission(target)
            if prepared is None:
                continue
            meta, submission_kwargs = prepared
            try:
                returned_run_id = str(config.invocation_backend_service.run_submission(**submission_kwargs) or '').strip()
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
                    future = pool.submit(config.invocation_backend_service.run_submission, **submission_kwargs)
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
    requested_build_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    invocation_id: str,
    invocation_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> bool:
    batch_id = str(invocation_id or targets[0].get('run_id') or 'invocation').strip() if targets else 'invocation'
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_execute_batch_worker(
                problem=problem,
                user=user,
                requested_build_id=requested_build_id,
                run_mode=run_mode,
                targets=targets,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
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
        queue_name='invocation',
        backend=config.invocation_backend_service.active_backend_name(),
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
    invocation_id: str,
    verification_signature: str='',
    verification_signature_details: dict[str, str] | None=None,
) -> None:
    planned_run_ids: list[str] = []
    for target in targets:
        token = normalize_run_id_token(target.get('run_id'))
        if token and token not in planned_run_ids:
            planned_run_ids.append(token)
    run_ids: list[str] = list(planned_run_ids)
    run_id = run_ids[0] if run_ids else ''
    build_id = _C.RUN_PLACEHOLDER_BUILD_ID
    verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    ws_row = config.db.fetch_one('SELECT path FROM workspaces WHERE id=? AND problem_id=?', [int(workspace_id), int(problem_id)])
    if ws_row is not None:
        try:
            workspace_path = Path(str(ws_row['path'] or '')).resolve()
            _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
            verification_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        except Exception:
            verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    verification_details: dict[str, object] = {'status': 'failed', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'submission_paths': [str(item.get('path') or '') for item in targets], 'solution_count': len(targets), 'invocation_id': invocation_id, 'run_id': run_id, 'run_ids': list(run_ids), 'run_count': len(run_ids), 'invocation_backend': config.invocation_backend_service.active_backend_name(), 'error': ''}
    verification_details['mode'] = verification_mode
    if verification_signature:
        verification_details['verification_signature'] = verification_signature
    if isinstance(verification_signature_details, dict) and verification_signature_details:
        verification_details['verification_signature_details'] = dict(verification_signature_details)

    def _backfill_missing_verification_runs(
        error_text: str,
        *,
        build_for_failure: str,
        execution_skipped_for_missing: bool=False,
    ) -> None:
        safe_error = str(error_text or '').strip() or 'verification failed'
        safe_build_for_failure = str(build_for_failure or _C.RUN_PLACEHOLDER_BUILD_ID).strip() or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            token = normalize_run_id_token(target.get('run_id'))
            if not token:
                continue
            if token not in run_ids:
                run_ids.append(token)
            existing = config.db.fetch_one(
                'SELECT id FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                [token, int(problem_id), int(workspace_id)],
            )
            if existing is not None:
                continue
            record_async_run_failure(
                problem,
                user,
                token,
                mode=verification_mode,
                source_label=str(target.get('path') or ''),
                error=safe_error,
                build_id=safe_build_for_failure,
                invocation_id=invocation_id,
                invocation_run_ids=run_ids,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                invocation_source='verification.start',
                synthesize_failed_tests=not bool(execution_skipped_for_missing),
                failure_stage='build' if execution_skipped_for_missing else '',
                execution_skipped=bool(execution_skipped_for_missing),
            )
        deduped = dedupe_preserve_order(run_ids)
        run_ids.clear()
        run_ids.extend(deduped)

    try:
        if ws_row is None:
            raise RuntimeError('workspace metadata missing')
        workspace_path = Path(str(ws_row['path'] or '')).resolve()
        if (not workspace_path.exists()) or (not workspace_path.is_dir()) or workspace_path.is_symlink():
            raise RuntimeError('workspace path is unavailable')
        local_ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
        build_id, _implicit_created = _ensure_implicit_build(
            problem,
            user,
            ctx=local_ctx,
            for_verification=True,
        )
        verification_details['build_id'] = build_id
        verification_details['build_status'] = 'ok'
        verification_details['source_commit'] = str(workspace_head or '').strip()
        verification_details['source_ref'] = str(workspace_head or '').strip()
        verification_details['build_error'] = ''
        verification_details['build_failed_step'] = ''
        verification_details['build_failed_test'] = ''
        accepted_source_path = ''
        try:
            solution_entries, _truncated = list_solution_entries(workspace_path)
            accepted_source_path = str(
                resolve_build_accepted_solution_source(workspace_path, solution_entries)
            ).strip()
        except Exception:
            accepted_source_path = ''
        buildsolve_row: dict[str, object] | None = None
        buildsolve_summary: dict | None = None
        if accepted_source_path:
            buildsolve_row, buildsolve_summary = _find_buildsolve_run(
                problem_id,
                workspace_id,
                build_id,
                accepted_source_path,
            )
        target_specs: list[dict[str, object]] = []
        for index, target in enumerate(targets):
            source_path = str(target.get('path') or '').strip()
            expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
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
                    'requested_run_id': normalize_run_id_token(target.get('run_id')),
                    'reuse_buildsolve': reuse_buildsolve,
                }
            )
        solution_results_by_index: dict[int, dict[str, object]] = {}
        cancel_reason = 'verification cancelled by user'
        cancel_requested = False

        def _invocation_cancel_requested() -> bool:
            nonlocal cancel_requested
            if cancel_requested:
                return True
            if _invocation_marked_cancelled(problem_id, actor_user_id, invocation_id):
                cancel_requested = True
            return cancel_requested

        parallelism = _invocation_submission_parallelism(len(target_specs))
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
                    if _invocation_cancel_requested():
                        cancel_run_id = requested_run_id or _allocate_run_id()
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = cancel_run_id
                        if cancel_run_id not in run_ids:
                            run_ids.append(cancel_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        run_row = config.db.fetch_one(
                            'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                            [cancel_run_id, problem_id, workspace_id],
                        )
                        if not _run_marked_cancelled(problem_id, workspace_id, cancel_run_id):
                            record_async_run_failure(
                                problem,
                                user,
                                cancel_run_id,
                                mode=verification_mode,
                                source_label=source_path,
                                error=cancel_reason,
                                build_id=build_id,
                                invocation_id=invocation_id,
                                invocation_run_ids=run_ids,
                                expected_behavior=expected_behavior,
                                invocation_source='verification.start',
                                synthesize_failed_tests=False,
                                failure_stage='cancel',
                                execution_skipped=True,
                            )
                            run_row = config.db.fetch_one(
                                'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                                [cancel_run_id, problem_id, workspace_id],
                            )
                        summary_obj = parse_summary_json(
                            run_row['summary_json'] if run_row is not None else None,
                            f'verification/{cancel_run_id}',
                        )
                        _store_target_result(spec, current_run_id=cancel_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    if requested_run_id and _run_marked_cancelled(problem_id, workspace_id, requested_run_id):
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = requested_run_id
                        if requested_run_id not in run_ids:
                            run_ids.append(requested_run_id)
                        run_ids = dedupe_preserve_order(run_ids)
                        run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [requested_run_id, problem_id, workspace_id])
                        summary_obj = parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{requested_run_id}')
                        _store_target_result(spec, current_run_id=requested_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    if bool(spec.get('reuse_buildsolve')):
                        if buildsolve_row is None or not isinstance(buildsolve_summary, dict):
                            raise RuntimeError('build.solve result missing for accepted solution reuse')
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
                            invocation_id=invocation_id,
                            invocation_run_ids=run_ids,
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
                    invocation_run_ids_snapshot = list(run_ids)
                    future = pool.submit(
                        config.invocation_backend_service.run_submission,
                        problem=problem,
                        username=user,
                        build_id=build_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=submission_run_id,
                        invocation_id=invocation_id,
                        invocation_run_ids=invocation_run_ids_snapshot,
                        expected_behavior=expected_behavior,
                        invocation_source='verification.start',
                        task_kind='solve',
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
                        run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [current_run_id, problem_id, workspace_id])
                        if run_row is None:
                            raise RuntimeError('run metadata missing after submission')
                        summary_obj = parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{current_run_id}')
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
                                    build_id=build_id,
                                    invocation_id=invocation_id,
                                    invocation_run_ids=run_ids,
                                    expected_behavior=expected_behavior,
                                    invocation_source='verification.start',
                                )
                            run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [fallback_run_id, problem_id, workspace_id])
                            summary_obj = parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{fallback_run_id}')
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
            if _invocation_cancel_requested():
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
                    config.invocation_backend_service.run_submission(
                        problem=problem,
                        username=user,
                        build_id=build_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=retry_run_id,
                        invocation_id=invocation_id,
                        invocation_run_ids=list(run_ids),
                        expected_behavior='accepted',
                        invocation_source='verification.start',
                        task_kind='solve',
                    )
                    or ''
                ).strip()
                normalized_submitted = normalize_run_id_token(submitted_run_id)
                if normalized_submitted:
                    retry_run_id = normalized_submitted
                run_row = config.db.fetch_one(
                    'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                    [retry_run_id, problem_id, workspace_id],
                )
                if run_row is None:
                    continue
                summary_obj = parse_summary_json(
                    run_row['summary_json'] if run_row is not None else None,
                    f'verification/{retry_run_id}',
                )
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
            if not bool(item.get('matched')) and (not first_reason):
                first_reason = _verification_solution_failure_hint(
                    str(item.get('source_path') or ''),
                    str(item.get('reason') or ''),
                    str(item.get('error') or ''),
                )
            solution_results.append(item)
        run_id = run_ids[0] if run_ids else ''
        verification_details['run_id'] = run_id
        verification_details['run_ids'] = list(run_ids)
        verification_details['solutions'] = solution_results
        verification_details['run_count'] = len(run_ids)
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
        verification_details['status'] = 'pass' if passed else 'failed'
        if not passed and first_reason:
            verification_details['error'] = first_reason
        for item in solution_results:
            current_run_id = str(item.get('run_id') or '').strip()
            if not current_run_id:
                continue
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            annotated = _annotate_run_invocation_result(problem_id, workspace_id, current_run_id, invocation_id=invocation_id, invocation_run_ids=run_ids, expected_behavior=expected_behavior, invocation_source='verification.start')
            item['matched'] = bool(annotated.get('matched'))
            item['completed'] = bool(annotated.get('completed'))
            item['passed_all_tests'] = bool(annotated.get('passed_all_tests'))
            item['reason'] = str(annotated.get('reason') or item.get('reason') or '')
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
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
        if run_id:
            run_row = config.db.fetch_one('SELECT summary_json FROM runs WHERE id=?', [run_id])
            run_summary_obj = parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{run_id}/status')
            if not isinstance(run_summary_obj, dict):
                run_summary_obj = {}
            run_summary_obj['verification'] = {'source': 'sidebar', 'status': verification_details['status'], 'steps': verification_details['steps'], 'build_id': build_id, 'submission_paths': verification_details.get('submission_paths', []), 'run_ids': run_ids, 'source_commit': verification_details.get('source_commit', ''), 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty}
            config.db.execute('UPDATE runs SET summary_json=? WHERE id=?', [json.dumps(run_summary_obj), run_id])
    except Exception as exc:
        safe_build_status = str(verification_details.get('build_status') or '').strip().lower()
        build_stage_failed = safe_build_status in {'failed', 'error', 'missing'} or str(exc).strip().lower().startswith('build failed')
        _backfill_missing_verification_runs(
            str(exc),
            build_for_failure=build_id,
            execution_skipped_for_missing=build_stage_failed,
        )
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
    run_id = run_ids[0] if run_ids else ''
    verification_details['run_id'] = run_id
    verification_details['run_ids'] = list(run_ids)
    verification_details['run_count'] = len(run_ids)
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
    invocation_id: str,
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
                invocation_id=invocation_id,
                verification_signature=verification_signature,
                verification_signature_details=verification_signature_details,
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.verification_lock:
                    config.verification_workers.discard(worker)
                    config.verification_inflight.discard(key)
    thread_name = invocation_id if invocation_id else key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            backend=config.invocation_backend_service.active_backend_name(),
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

def _run_export_create_worker(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_build_id: str, requested_export_type: str) -> None:
    safe_requested_build_id = str(requested_build_id or '').strip()
    safe_export_type = str(requested_export_type or '').strip().lower() or 'icpc'
    details: dict[str, object] = {'status': 'failed', 'build_id': safe_requested_build_id, 'export_type': safe_export_type, 'source_commit': str(head_commit or '').strip(), 'filename': '', 'error': ''}
    worker_error: Exception | None = None
    try:
        if safe_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        safe_head = str(head_commit or '').strip()
        if not safe_head:
            raise ValueError('no committed revision; commit changes first')
        resolved_build_id = safe_requested_build_id
        if not resolved_build_id:
            active_build = latest_workspace_committed_build(int(problem_id), int(workspace_id), safe_head, ok_only=True)
            if active_build is None:
                resolved_build_id = config.build_service.run_build(problem, user, commit=safe_head, ref=safe_head)
            else:
                resolved_build_id = str(active_build['id'] or '').strip()
        if not resolved_build_id:
            raise RuntimeError('failed to resolve build id for export')
        build_row = config.db.fetch_one('SELECT status,source_commit,source_ref FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [resolved_build_id, int(problem_id), int(workspace_id)])
        if build_row is None:
            raise ValueError(f'build metadata not found: {resolved_build_id}')
        build_status = str(build_row['status'] or 'missing').strip().lower()
        source_commit = str(build_row['source_commit'] or '').strip()
        source_ref = str(build_row['source_ref'] or '').strip()
        details['build_id'] = resolved_build_id
        details['build_status'] = build_status
        details['source_commit'] = source_commit
        details['source_ref'] = source_ref
        if source_commit != safe_head or source_ref != safe_head:
            raise ValueError('package must be generated from current committed revision')
        if build_status != 'ok':
            raise ValueError(f'build status is {build_status}')
        out = config.export_service.create_export(problem, resolved_build_id, safe_export_type)
        details['status'] = 'ok'
        details['filename'] = out.name
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        worker_error = exc
    audit(actor_user_id, problem_id, 'export.create', details)
    if worker_error is not None:
        raise worker_error

def start_export_job(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_build_id: str, requested_export_type: str, initial_details: dict[str, object] | None=None) -> bool:
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
            _run_export_create_worker(problem, user, actor_user_id=actor_user_id, problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_build_id=requested_build_id, requested_export_type=requested_export_type)
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
            backend=config.invocation_backend_service.active_backend_name(),
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





