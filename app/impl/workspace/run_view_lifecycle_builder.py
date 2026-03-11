from __future__ import annotations

from pathlib import Path

from app.impl.runtime.config import config
from app.main_util import preserve_error_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.platform.process import is_canonical_artifact_id

from .context import count_label
from .context_operation import parse_summary_json, _status_rule_expectation_display
from .context_job import _invocation_marked_cancelled
from .run_view_lifecycle_card import (
    normalize_run_id_token,
    _normalize_verification_step_id,
    _run_lifecycle_current_step,
    _run_lifecycle_current_step_fields,
    _run_lifecycle_status_label,
    _verification_buildsolve_case_progress,
    _verification_failed_build_step_id,
    _verification_output_stats,
    _verification_run_test_progress,
    _verification_step_title,
    _verification_tests_meta_stats,
    _verification_validate_stats,
)

_C = config.constants


def _build_verification_lifecycle_card(
    *,
    problem_slug: str,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    invocation_id: str,
    verification_details: dict[str, object],
    columns: list[dict[str, object]],
    detail_status: str,
    detail_running: bool,
    progress_reported: int,
    progress_total: int,
    matched_count: int,
    match_total: int,
) -> dict[str, object]:
    raw_steps = verification_details.get('steps')
    step_ids: list[str] = []
    if isinstance(raw_steps, list):
        for item in raw_steps:
            token = _normalize_verification_step_id(item)
            if token and token not in step_ids:
                step_ids.append(token)
    if not step_ids:
        step_ids = ['gen', 'val', 'run', 'check']
    if 'run' not in step_ids:
        step_ids.append('run')
    if 'check' not in step_ids:
        step_ids.append('check')
    status_by_step = {token: 'pending' for token in step_ids}
    detail_by_step: dict[str, str] = {}

    build_id = str(verification_details.get('build_id') or '').strip()
    if (not is_canonical_artifact_id(build_id)):
        build_id = ''
        for col in columns:
            candidate = str(col.get('build_id') or '').strip()
            if is_canonical_artifact_id(candidate):
                build_id = candidate
                break
    build_status = str(verification_details.get('build_status') or '').strip().lower()
    has_materialized_columns = any(
        bool(col.get('has_run_row')) for col in columns if isinstance(col, dict)
    )
    if (not build_id) and bool(detail_running) and (not has_materialized_columns):
        inflight_build = config.db.fetch_one(
            """
            SELECT id,status
            FROM builds
            WHERE problem_id=? AND workspace_id=? AND status IN ('running','queued','pending')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [int(problem_id), int(workspace_id)],
        )
        if inflight_build is not None:
            candidate = str(inflight_build['id'] or '').strip()
            if is_canonical_artifact_id(candidate):
                build_id = candidate
                if not str(build_status or '').strip():
                    build_status = str(inflight_build['status'] or '').strip().lower()
    build_failed_step = str(verification_details.get('build_failed_step') or '').strip()
    build_failed_test = str(verification_details.get('build_failed_test') or '').strip()
    build_error_text = preserve_error_text(str(verification_details.get('build_error') or ''))
    if build_id:
        build_row = config.db.fetch_one(
            'SELECT status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?',
            [build_id, int(problem_id), int(workspace_id)],
        )
        if build_row is not None:
            status_token = str(build_row['status'] or '').strip().lower()
            if status_token:
                build_status = status_token
            if not build_failed_step:
                build_summary = parse_summary_json(build_row['summary_json'], f'verification/build/{build_id}')
                build_failed_step = str(build_summary.get('failed_step') or '').strip() if isinstance(build_summary, dict) else ''
                build_failed_test = str(build_summary.get('failed_test') or '').strip() if isinstance(build_summary, dict) else build_failed_test
                if (not build_error_text):
                    build_error_text = preserve_error_text(str(build_summary.get('error') or '')) if isinstance(build_summary, dict) else ''
    if build_id:
        detail_by_step['gen'] = 'build prepared'

    running_statuses = {'running', 'queued', 'pending'}
    materialized_columns = [
        col
        for col in columns
        if isinstance(col, dict) and bool(col.get('has_run_row'))
    ]
    run_statuses = [str(col.get('status') or '').strip().lower() for col in materialized_columns]
    has_started_runs = bool(materialized_columns)
    has_running_runs = any((item in running_statuses for item in run_statuses))

    # Backend routing is judgehost-only after migration cleanup; lifecycle
    # progress should always follow case-level judgehost progress semantics.
    prefer_case_progress = True
    invocation_sources: set[str] = set()
    for col in materialized_columns:
        summary_obj = col.get('summary')
        if not isinstance(summary_obj, dict):
            continue
        inv_block = summary_obj.get('invocation')
        if not isinstance(inv_block, dict):
            continue
        source_token = str(inv_block.get('source') or '').strip().lower()
        if source_token:
            invocation_sources.add(source_token)
    if not invocation_sources and isinstance(verification_details, dict):
        details_source = str(verification_details.get('source') or '').strip().lower()
        if details_source:
            invocation_sources.add(details_source)
    buildsolve_only = bool(invocation_sources) and invocation_sources.issubset({'build.solve'})
    if buildsolve_only:
        has_started_runs = False
        has_running_runs = False
    if has_started_runs and bool(detail_running):
        has_running_runs = True
    completed_runs = sum((1 for item in run_statuses if item and item not in running_statuses))
    failed_run_count = 0
    cancelled_run_count = 0
    for idx, col in enumerate(materialized_columns):
        run_status = run_statuses[idx] if idx < len(run_statuses) else str(col.get('status') or '').strip().lower()
        summary_obj = col.get('summary')
        cancelled_this_run = False
        if isinstance(summary_obj, dict):
            if bool(summary_obj.get('cancelled')):
                cancelled_this_run = True
            else:
                summary_error_text = str(summary_obj.get('error') or '').strip().lower()
                if ('cancelled by user' in summary_error_text) or ('verification cancelled' in summary_error_text):
                    cancelled_this_run = True
        if cancelled_this_run:
            cancelled_run_count += 1
            continue
        if run_status == 'failed':
            failed_run_count += 1
    run_skip_flags = [bool(col.get('execution_skipped')) for col in columns if isinstance(col, dict)]
    all_runs_skipped = bool(run_skip_flags) and all(run_skip_flags)
    safe_detail_status = str(detail_status or '').strip().lower()
    error_text = preserve_error_text(str(verification_details.get('error') or ''))
    error_text_lower = error_text.lower()
    cancelled_from_error = ('cancelled by user' in error_text_lower) or ('verification cancelled' in error_text_lower)
    cancelled_from_details = bool(verification_details.get('cancelled'))
    cancelled_from_audit = False
    safe_invocation_id = normalize_run_id_token(invocation_id)
    if safe_invocation_id and int(actor_user_id) > 0:
        try:
            cancelled_from_audit = _invocation_marked_cancelled(int(problem_id), int(actor_user_id), safe_invocation_id)
        except Exception:
            cancelled_from_audit = False
    run_interrupted = bool(cancelled_run_count > 0 or cancelled_from_error or cancelled_from_details or cancelled_from_audit)
    try:
        run_count = max(0, int(verification_details.get('run_count') or 0))
    except Exception:
        run_count = 0
    if run_count <= 0:
        run_count = len(columns)
    run_count = max(run_count, len(columns))
    has_any_runs = bool(run_count > 0 and (has_started_runs or build_status in {'ok', 'failed', 'error'}))
    if has_any_runs:
        completed_runs = min(run_count, max(0, int(completed_runs)))
    test_progress = _verification_run_test_progress(
        materialized_columns=materialized_columns,
        run_statuses=run_statuses,
        run_count=run_count,
        fallback_tests_per_solution=max(0, int(progress_total)),
    )
    total_test_units = int(test_progress.get('total') or 0)
    completed_test_units = int(test_progress.get('completed') or 0)
    running_test_units = int(test_progress.get('running') or 0)
    run_failed = False
    if (not all_runs_skipped) and has_started_runs and (not has_running_runs) and (not run_interrupted):
        if failed_run_count > 0:
            run_failed = True
        elif safe_detail_status == 'failed':
            run_failed = True
    run_failure_detail_text = ""
    if run_failed or run_interrupted:
        for idx, col in enumerate(materialized_columns):
            run_status = run_statuses[idx] if idx < len(run_statuses) else str(col.get("status") or "").strip().lower()
            if run_status in {"running", "queued", "pending"}:
                continue
            summary_obj = col.get("summary")
            if not isinstance(summary_obj, dict):
                continue
            error_candidate = preserve_error_text(str(summary_obj.get("error") or ""), max_chars=1600, max_lines=24)
            if not error_candidate:
                continue
            source_label = str(col.get("title") or "").strip()
            if source_label and (not error_candidate.startswith(f"{source_label}:")):
                error_candidate = f"{source_label}: {error_candidate}"
            run_failure_detail_text = error_candidate
            break
    progress_label = ''
    if all_runs_skipped:
        if run_interrupted:
            progress_label = 'not executed (cancelled)'
        else:
            progress_label = 'not executed (build failed)'
    elif total_test_units > 0 and has_started_runs and has_running_runs:
        progress_label = f'{completed_test_units}/{total_test_units} tests finished'
    elif run_interrupted:
        if total_test_units > 0:
            progress_label = f'failed ({completed_test_units}/{total_test_units} completed)'
        elif run_count > 0:
            progress_label = f'failed ({completed_runs}/{run_count} completed)'
        else:
            progress_label = 'failed'
    elif run_failed:
        if total_test_units > 0:
            progress_label = f'failed ({completed_test_units}/{total_test_units} completed)'
        elif run_count > 0:
            progress_label = f'failed ({completed_runs}/{run_count} completed)'
        else:
            progress_label = 'failed'
    elif total_test_units > 0 and has_started_runs:
        progress_label = f'{completed_test_units}/{total_test_units} tests finished'
    elif build_status == 'ok' and total_test_units > 0:
        progress_label = f'0/{total_test_units} tests finished'
    elif has_started_runs:
        progress_label = f'{completed_runs}/{run_count} solutions finished'
    elif build_status == 'ok' and run_count > 0:
        progress_label = f'0/{run_count} solutions finished'
    if progress_label:
        detail_by_step['run'] = progress_label
    if match_total > 0:
        detail_by_step['check'] = f'matched expectations {int(matched_count)}/{int(match_total)}'
    if error_text and (build_status not in {'failed', 'error'}) and (not run_interrupted):
        detail_by_step['check'] = error_text

    tests_meta_stats = _verification_tests_meta_stats(problem_slug, build_id)
    tests_meta_loaded = bool(tests_meta_stats.get('loaded'))
    generated_total = max(0, int(tests_meta_stats.get('total') or 0))
    generated_manual = max(0, int(tests_meta_stats.get('manual') or 0))
    generated_from_gen = max(0, int(tests_meta_stats.get('gen') or 0))
    if generated_total <= 0 and progress_total > 0:
        generated_total = max(0, int(progress_total))

    output_stats = _verification_output_stats(problem_slug, build_id)
    outputs_total = max(0, int(output_stats.get('total') or 0))
    outputs_generated = max(0, int(output_stats.get('generated') or 0))
    buildsolve_case_total = 0
    buildsolve_case_reported = 0
    if build_id:
        buildsolve_progress = _verification_buildsolve_case_progress(build_id)
        case_total = max(0, int(buildsolve_progress.get('total') or 0))
        case_reported = max(0, int(buildsolve_progress.get('reported') or 0))
        buildsolve_case_total = case_total
        buildsolve_case_reported = case_reported
        if case_total > 0:
            if build_status in {'running', 'queued', 'pending'}:
                # While build is running, case-level progress is authoritative for
                # output-generation progress. File-based ans counts can include stale
                # artifacts from previous attempts.
                outputs_total = case_total
                outputs_generated = min(case_reported, case_total)
            else:
                outputs_total = max(outputs_total, case_total)
                outputs_generated = max(outputs_generated, min(case_reported, case_total))
    if outputs_total <= 0 and generated_total > 0:
        outputs_total = generated_total
    if outputs_generated > outputs_total:
        outputs_total = outputs_generated
    if (
        build_status in {'running', 'queued', 'pending'}
        and prefer_case_progress
        and (not has_started_runs)
        and build_id
        and buildsolve_case_total <= 0
    ):
        # Judgehost case progress has not been registered yet; avoid showing stale
        # ans-file totals from previous runs (for example transient 27/27 while still running).
        outputs_total = 0
        outputs_generated = 0
    validate_stats = _verification_validate_stats(problem_slug, build_id)
    validated_total = max(0, int(validate_stats.get('total') or 0))
    validated_ok = max(0, int(validate_stats.get('ok') or 0))
    validated_failed = max(0, int(validate_stats.get('failed') or 0))
    validated_timed_out = max(0, int(validate_stats.get('timed_out') or 0))
    if validated_total <= 0 and build_status == 'ok' and generated_total > 0:
        validated_total = generated_total
        validated_ok = generated_total
        validated_failed = 0
        validated_timed_out = 0

    build_failed = build_status in {'failed', 'error'}
    build_running = build_status in {'running', 'queued', 'pending'}
    build_done = build_status == 'ok'
    if (not build_status) and has_started_runs:
        build_done = True
    if (not build_status) and (not has_started_runs):
        # Initial verification audit entry is written before build starts; keep
        # lifecycle focused on "Generate Inputs" instead of jumping to run stage.
        build_running = True
    outputs_phase_done_while_build_running = bool(has_started_runs)
    if (not outputs_phase_done_while_build_running) and buildsolve_case_total > 0:
        outputs_phase_done_while_build_running = buildsolve_case_reported >= buildsolve_case_total

    failed_step_id = ''
    cancel_before_runs = run_interrupted and (all_runs_skipped or (not has_started_runs))
    if cancel_before_runs:
        cancel_detail = error_text or 'verification cancelled by user'
        # If generation has already produced tests (or build is already past initial
        # bootstrap), keep cancellation pinned to step-2 instead of regressing to step-1.
        if ('val' in step_ids) and (
            generated_total > 0
            or outputs_total > 0
            or validated_total > 0
            or build_status in {'ok', 'running', 'queued', 'pending'}
            or bool(build_id)
        ):
            failed_step_id = 'val'
        elif 'gen' in step_ids:
            failed_step_id = 'gen'
        elif 'val' in step_ids:
            failed_step_id = 'val'
        else:
            failed_step_id = step_ids[0]
        fail_index = step_ids.index(failed_step_id) if failed_step_id in step_ids else 0
        for idx, token in enumerate(step_ids):
            if idx < fail_index:
                status_by_step[token] = 'done'
            elif idx == fail_index:
                status_by_step[token] = 'failed'
            else:
                status_by_step[token] = 'skipped'
        if failed_step_id:
            detail_by_step[failed_step_id] = cancel_detail
        if 'run' in status_by_step:
            detail_by_step['run'] = 'not executed (cancelled)'
        if 'check' in status_by_step:
            detail_by_step['check'] = 'failed'
    elif build_failed:
        failed_step_id = _verification_failed_build_step_id(build_failed_step, step_ids)
        fail_index = step_ids.index(failed_step_id) if failed_step_id in step_ids else 0
        if error_text:
            safe_failed_hint = str(build_failed_step or '').strip().lower()
            if failed_step_id == 'val' and 'solve' in safe_failed_hint:
                if build_failed_test:
                    detail_by_step[failed_step_id] = f'output generation failed on {build_failed_test}'
                else:
                    detail_by_step[failed_step_id] = 'output generation failed'
            else:
                detail_by_step[failed_step_id] = error_text
        for idx, token in enumerate(step_ids):
            if idx < fail_index:
                status_by_step[token] = 'done'
            elif idx == fail_index:
                status_by_step[token] = 'failed'
            else:
                status_by_step[token] = 'skipped'
                if token == 'run':
                    detail_by_step[token] = 'not executed (build failed)'
                if token == 'check':
                    detail_by_step[token] = 'skipped'
    elif build_running:
        if generated_total > 0 or outputs_total > 0 or has_started_runs:
            if 'gen' in status_by_step:
                status_by_step['gen'] = 'done'
        if outputs_phase_done_while_build_running:
            if 'val' in status_by_step:
                status_by_step['val'] = 'done'
            if 'run' in status_by_step:
                if all_runs_skipped:
                    status_by_step['run'] = 'skipped'
                elif has_running_runs:
                    status_by_step['run'] = 'running'
                elif run_interrupted or run_failed:
                    status_by_step['run'] = 'failed'
                elif has_started_runs:
                    status_by_step['run'] = 'done'
                else:
                    status_by_step['run'] = 'pending'
            if 'check' in status_by_step:
                if run_interrupted:
                    status_by_step['check'] = 'skipped'
                    detail_by_step['check'] = 'failed'
                elif has_running_runs:
                    status_by_step['check'] = 'pending'
                elif safe_detail_status == 'ok' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'done'
                elif safe_detail_status == 'failed' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'failed'
                elif has_started_runs:
                    status_by_step['check'] = 'done'
                else:
                    status_by_step['check'] = 'pending'
                if status_by_step['check'] != 'pending':
                    check_idx = step_ids.index('check')
                    for token in step_ids[:check_idx]:
                        if status_by_step[token] == 'pending':
                            status_by_step[token] = 'done'
        elif generated_total > 0 or outputs_total > 0:
            if 'val' in status_by_step:
                status_by_step['val'] = 'running'
            else:
                for token in step_ids:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'running'
                        break
        else:
            running_step = 'gen' if 'gen' in status_by_step else step_ids[0]
            status_by_step[running_step] = 'running'
    else:
        if build_done:
            if 'gen' in status_by_step:
                status_by_step['gen'] = 'done'
            if 'val' in status_by_step:
                status_by_step['val'] = 'done'
            run_idx = step_ids.index('run') if 'run' in step_ids else -1
            if run_idx > 0:
                for token in step_ids[:run_idx]:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'done'
        if build_done and has_running_runs:
            if 'run' in status_by_step:
                status_by_step['run'] = 'running'
            else:
                for token in step_ids:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'running'
                        break
        else:
            if build_done and has_any_runs and 'run' in status_by_step:
                if all_runs_skipped:
                    status_by_step['run'] = 'skipped'
                elif run_interrupted:
                    status_by_step['run'] = 'failed'
                elif run_failed:
                    status_by_step['run'] = 'failed'
                elif has_started_runs:
                    status_by_step['run'] = 'done'
                else:
                    status_by_step['run'] = 'pending'
            if 'check' in status_by_step:
                if run_interrupted:
                    status_by_step['check'] = 'skipped'
                    detail_by_step['check'] = 'failed'
                elif safe_detail_status == 'ok' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'done'
                elif safe_detail_status == 'failed' and (has_started_runs or all_runs_skipped or build_failed):
                    status_by_step['check'] = 'failed'
                elif has_started_runs:
                    status_by_step['check'] = 'done'
                else:
                    status_by_step['check'] = 'pending'
                if status_by_step['check'] != 'pending':
                    check_idx = step_ids.index('check')
                    for token in step_ids[:check_idx]:
                        if status_by_step[token] == 'pending':
                            status_by_step[token] = 'done'

    step_facts: dict[str, list[dict[str, str]]] = {token: [] for token in step_ids}
    step_notes: dict[str, list[str]] = {token: [] for token in step_ids}

    def _step_add_fact(step_id: str, label: str, value: object, tone: str='') -> None:
        token = str(step_id or '').strip()
        if token not in step_facts:
            return
        label_text = str(label or '').strip()
        value_text = str(value or '').strip()
        if (not label_text) or (not value_text):
            return
        row = {'label': label_text, 'value': value_text, 'tone': str(tone or '').strip().lower()}
        step_facts[token].append(row)

    def _step_add_note(step_id: str, text: object) -> None:
        token = str(step_id or '').strip()
        if token not in step_notes:
            return
        note_text = str(text or '').strip()
        if not note_text:
            return
        if note_text not in step_notes[token]:
            step_notes[token].append(note_text)

    gen_status = str(status_by_step.get('gen') or '').strip().lower()
    if gen_status != 'failed':
        if generated_total > 0:
            generated_count_label = count_label(generated_total, 'test')
            detail_by_step['gen'] = f'generated {generated_count_label}'
        elif build_running and tests_meta_loaded:
            detail_by_step['gen'] = 'generating inputs'

    val_status_token = str(status_by_step.get('val') or '').strip().lower()
    if val_status_token != 'failed':
        if outputs_total > 0:
            if (
                (val_status_token in {'pending', 'running'})
                and build_running
                and ((outputs_generated < outputs_total) or (not has_started_runs))
            ):
                detail_by_step['val'] = 'generating outputs'
            else:
                detail_by_step['val'] = f'generated outputs {outputs_generated}/{outputs_total}'
        elif build_running and generated_total > 0:
            detail_by_step['val'] = 'generating outputs'

    running_count = sum((1 for token in run_statuses if token in running_statuses))
    finished_count = max(0, int(completed_runs))
    if all_runs_skipped:
        if run_count > 0:
            _step_add_fact('run', 'Solutions skipped', f'{int(run_count)}/{int(run_count)}')
    else:
        if run_count > 0:
            if run_interrupted or run_failed:
                _step_add_fact('run', 'Solutions completed', f'{finished_count}/{int(run_count)}')
            else:
                _step_add_fact('run', 'Solutions finished', f'{finished_count}/{int(run_count)}')
        if total_test_units > 0:
            if run_interrupted or run_failed:
                _step_add_fact('run', 'Tests completed', f'{completed_test_units}/{total_test_units}')
            else:
                _step_add_fact('run', 'Tests finished', f'{completed_test_units}/{total_test_units}')
        if running_count > 0:
            _step_add_fact('run', 'Running solutions', count_label(running_count, 'solution'))
        if running_test_units > 0:
            _step_add_fact('run', 'Running tests', count_label(running_test_units, 'test'))
        if failed_run_count > 0:
            _step_add_fact('run', 'Failed solutions', count_label(failed_run_count, 'solution'))
        if cancelled_run_count > 0:
            _step_add_fact('run', 'Cancelled solutions', count_label(cancelled_run_count, 'solution'))
        if progress_total > 0:
            _step_add_fact('run', 'Tests per solution', count_label(int(progress_total), 'test'))
    if all_runs_skipped:
        run_skip_note = str(detail_by_step.get('run') or '').strip() or 'not executed (build failed)'
        _step_add_note('run', run_skip_note)
    elif run_interrupted:
        _step_add_note('run', run_failure_detail_text or error_text or 'verification cancelled by user')
    elif run_failed and (run_failure_detail_text or error_text):
        _step_add_note('run', run_failure_detail_text or error_text)
    elif build_status == 'ok' and run_count > 0 and (not has_started_runs):
        _step_add_note('run', 'Waiting for solution execution results.')

    if generated_total > 0 or (build_running and tests_meta_loaded):
        _step_add_fact('gen', 'Generated tests', count_label(generated_total, 'test'))
        if generated_manual > 0:
            _step_add_fact('gen', 'Manual tests', count_label(generated_manual, 'test'))
        if generated_from_gen > 0:
            _step_add_fact('gen', 'Generator tests', count_label(generated_from_gen, 'test'))
    elif build_running:
        _step_add_note('gen', 'Build is preparing input generation.')
    if build_failed and failed_step_id == 'gen':
        if build_failed_test:
            _step_add_note('gen', f'Failed test: {build_failed_test}')
        if build_error_text:
            _step_add_note('gen', build_error_text)
        elif error_text:
            _step_add_note('gen', error_text)

    if outputs_total > 0:
        hide_generated_outputs_fact = (
            (val_status_token in {'pending', 'running'})
            and (build_status in {'running', 'queued', 'pending'})
            and prefer_case_progress
            and (not has_started_runs)
            and bool(build_id)
            and buildsolve_case_total <= 0
        )
        if not hide_generated_outputs_fact:
            _step_add_fact('val', 'Generated outputs', f'{min(outputs_generated, outputs_total)}/{outputs_total}')

    if validated_total > 0:
        _step_add_fact('gen', 'Validated inputs', f'{validated_ok}/{validated_total}')
        if validated_failed > 0:
            _step_add_fact('gen', 'Failed validations', count_label(validated_failed, 'test'), tone='danger')
        if validated_timed_out > 0:
            _step_add_fact('gen', 'Validation timeouts', count_label(validated_timed_out, 'test'), tone='danger')
        if bool(validate_stats.get('truncated')):
            _step_add_note('gen', 'Validation log was truncated while summarizing.')
    elif build_status == 'ok' and generated_total > 0:
        _step_add_fact('gen', 'Validated inputs', f'{generated_total}/{generated_total}')
    elif (val_status_token in {'pending', 'running'}) and (build_status in {'running', 'queued', 'pending', ''}):
        if generated_total > 0:
            _step_add_note('val', 'Waiting for output generation results.')
        else:
            _step_add_note('val', 'Waiting for input generation results.')
    if (val_status_token in {'pending', 'running'}) and build_running and outputs_total > 0 and outputs_generated < outputs_total:
        _step_add_note('val', 'Running accepted solution to generate outputs.')
    if build_failed and failed_step_id == 'val':
        safe_failed_hint = str(build_failed_step or '').strip().lower()
        if build_failed_test:
            _step_add_note('val', f'Failed test: {build_failed_test}')
        if 'solve' in safe_failed_hint:
            if build_failed_test:
                _step_add_note('val', f'Output generation failed on {build_failed_test}')
            else:
                _step_add_note('val', 'Output generation failed.')
        elif build_error_text:
            _step_add_note('val', build_error_text)
        elif error_text:
            _step_add_note('val', error_text)

    if match_total > 0:
        _step_add_fact('check', 'Matched expectations', f'{int(matched_count)}/{int(match_total)}')
    if safe_detail_status:
        _step_add_fact('check', 'Overall status', safe_detail_status.upper(), tone='ok' if safe_detail_status == 'ok' else 'danger' if safe_detail_status == 'failed' else '')
    solutions_raw = verification_details.get('solutions')
    mismatch_sources: set[str] = set()
    if isinstance(solutions_raw, list) and solutions_raw:
        solution_total = 0
        solution_matched = 0
        mismatch_lines: list[str] = []
        for item in solutions_raw:
            if not isinstance(item, dict):
                continue
            solution_total += 1
            is_matched = bool(item.get('matched'))
            if is_matched:
                solution_matched += 1
                continue
            source_path = str(item.get('source_path') or '').strip()
            source_label = Path(source_path).name if source_path else f'solution {solution_total}'
            mismatch_sources.add(source_label)
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            rule_reason_text = preserve_error_text(str(item.get('reason') or ''), max_chars=600, max_lines=8)
            run_error_text = preserve_error_text(str(item.get('error') or ''), max_chars=1600, max_lines=24)
            if rule_reason_text and run_error_text:
                reason_text = f'{rule_reason_text}: {run_error_text}'
            elif rule_reason_text:
                reason_text = rule_reason_text
            elif run_error_text:
                reason_text = run_error_text
            else:
                reason_text = _status_rule_expectation_display(expected_behavior)
            line = f'{source_label}: {reason_text}'
            mismatch_lines.append(line)
        if solution_total > 0:
            _step_add_fact('check', 'Solutions matched', f'{solution_matched}/{solution_total}')
        for line in mismatch_lines[:4]:
            _step_add_note('check', line)
        hidden_mismatch = max(0, len(mismatch_lines) - 4)
        if hidden_mismatch > 0:
            _step_add_note('check', f'+{hidden_mismatch} more mismatches')
    if error_text:
        # verification_details.error is usually the first unmatched solution hint.
        # Avoid repeating it when per-solution mismatch notes already cover that source.
        redundant_with_solution_note = False
        if mismatch_sources and ': ' in error_text:
            error_source = error_text.split(': ', 1)[0].strip()
            if error_source and (error_source in mismatch_sources):
                redundant_with_solution_note = True
        if not redundant_with_solution_note:
            _step_add_note('check', error_text)

    steps: list[dict[str, object]] = []
    for idx, token in enumerate(step_ids, start=1):
        status_token = str(status_by_step.get(token) or 'pending')
        steps.append(
            {
                'index': idx,
                'id': token,
                'title': _verification_step_title(token),
                'status': status_token,
                'status_label': _run_lifecycle_status_label(status_token),
                'detail': str(detail_by_step.get(token) or '').strip(),
                'facts': step_facts.get(token) or [],
                'notes': step_notes.get(token) or [],
            }
        )
    current_step_index, current_step_title = _run_lifecycle_current_step(steps)
    current_step_status, current_step_status_label, current_step_detail = _run_lifecycle_current_step_fields(steps, current_step_index)
    return {
        'id': 'verification',
        'title': 'Verification Progress',
        'total_steps': len(steps),
        'current_step_index': current_step_index,
        'current_step_title': current_step_title,
        'current_step_status': current_step_status,
        'current_step_status_label': current_step_status_label,
        'current_step_detail': current_step_detail,
        'summary': '',
        'steps': steps,
    }



