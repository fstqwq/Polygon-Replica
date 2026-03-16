from __future__ import annotations
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import HTTPException
from app.impl.runtime.config import config
from .artifact import (
    safe_artifact_path,
    safe_run_artifact_path,
)
from .context import count_label
from .problem_config import read_problem_config
from .context_operation import (
    parse_summary_json,
    solution_metadata_entry,
    workspace_rel_file_exists,
)
from .context_run_detail import (
    _cap_run_test_feedback_files,
    _cap_summary_list,
    _decorate_compile_diagnostics,
    _interactive_transcript_preview,
    _normalize_diagnostics,
    _run_detail_preview_from_bytes,
    normalize_run_id_token,
    normalize_run_test_name_token,
    _run_detail_preview_from_path,
    _run_detail_preview_is_noise,
    _run_detail_preview_unavailable,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
    _run_source_from_summary,
)
from app.service.platform.error_text import (
    compact_error_text,
    preserve_error_text,
)
from app.service.platform.workspace_path import (
    normalize_optional_component_source_path_safe,
    normalize_workspace_rel_path,
    safe_workspace_path,
)
from app.service.verification.runtime import (
    effective_run_timeout_ms,
)
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from app.service.platform.process import is_canonical_artifact_id
from app.impl.workspace.run_view_lifecycle_builder import _build_verification_lifecycle_card
from app.impl.workspace.run_view_lifecycle_card import (
    _run_domjudge_case_cells,
    load_verification_detail_snapshot,
    _verification_tests_meta_stats,
)
from app.impl.workspace.context_verification import (
    _expected_status_rule,
    _status_rule_expected_display,
    _verification_solution_match,
)
from app.service.verification.store import (
    load_verification_record,
    load_verification_summary,
    verification_run,
    verification_run_ids,
    verification_stage_summary,
    verification_source_paths,
)
from app.impl.workspace.run_view_list import (
    _latest_iso_timestamp,
    _run_cell_kind,
    _run_expected_behavior_from_summary,
    _verification_source_from_summary,
    _is_main_correct_verification_source,
    _run_test_answer_name,
    _run_test_sort_key,
    _run_timeout_ms_from_summary,
)
from app.impl.workspace.run_display import (
    run_actual_display,
    run_actual_short,
    run_cpu_wall_ms_text,
    run_error_display,
    run_memory_mb_text,
    run_verdict_short,
)

_C = config.constants

def build_run_detail_context(
    ctx: dict,
    execute_mode: str,
    *,
    requested_verification_id: str = '',
    include_row_details: bool = False,
    detail_test_name: str = '',
) -> dict:
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    problem_id = int(ctx['problem']['id'])
    problem_slug = ctx['problem']['slug']
    username = ctx['user']['username']
    fallback_timeout_ms = 0
    try:
        _payload, general_cfg, _cfg_path = read_problem_config(workspace)
        fallback_timeout_ms = effective_run_timeout_ms(
            int(general_cfg.get('time_limit_ms') or _C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            mode=general_cfg.get('mode'),
            default_ms=int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            min_ms=int(_C.GENERAL_TIME_LIMIT_MIN_MS),
            max_ms=int(_C.GENERAL_TIME_LIMIT_MAX_MS),
            pass_fail_slack_sec=int(_C.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC),
            multi_pass_slack_sec=int(_C.RUN_WALL_TIME_SLACK_MULTI_PASS_SEC),
            interactive_slack_sec=int(_C.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC),
        )
    except Exception:
        fallback_timeout_ms = 0
    selected_ids: list[str] = []
    verification_run_rows: dict[str, dict[str, object]] = {}
    verification_id_hint = normalize_run_id_token(requested_verification_id)
    verification_details: dict[str, object] = {}
    verification_record = load_verification_record(config.db, verification_id_hint) if verification_id_hint else None
    if verification_record is not None and verification_record['workspace_id'] == workspace_id:
        verification_details = load_verification_summary(config.db, verification_id_hint)
        verification_details['verification_id'] = verification_id_hint
        verification_details['status'] = verification_record['status']
        verification_details['created_at'] = verification_record['created_at']
        verification_details['finished_at'] = verification_record['finished_at'] or ''
        if not selected_ids:
            for run_id in verification_run_ids(verification_details):
                token = normalize_run_id_token(run_id)
                if token:
                    selected_ids.append(token)
        for run_id in verification_run_ids(verification_details):
            run_token = normalize_run_id_token(run_id)
            if not run_token:
                continue
            run_row = verification_run(verification_details, run_id)
            if not run_row:
                continue
            verification_run_rows[run_token] = {
                'id': run_token,
                'artifact_verification_id': verification_details.get('artifact_verification_id') or verification_id_hint or '',
                'mode': verification_details.get('mode') or execute_mode,
                'status': run_row['status'],
                'source_label': run_row['source_label'],
                'summary': dict(run_row['summary']),
                'created_at': verification_record['created_at'],
                'finished_at': verification_record['finished_at'] or verification_details.get('finished_at') or '',
            }
    if not selected_ids and verification_details:
        for run_id in verification_run_ids(verification_details):
            token = normalize_run_id_token(run_id)
            if token and token not in selected_ids:
                selected_ids.append(token)
    verification_created_at = ''
    if not verification_created_at and verification_record is not None:
        verification_created_at = verification_record['created_at']
    expected_by_run_id: dict[str, str] = {}
    expected_by_source: dict[str, str] = {}
    solutions = verification_details.get('solutions') or []
    for item in solutions:
        expected_token = normalize_expected_behavior((item.get('expected_behavior') or 'unknown'))
        if expected_token == 'unknown':
            continue
        run_token = normalize_run_id_token(item.get('run_id'))
        if run_token and run_token not in expected_by_run_id:
            expected_by_run_id[run_token] = expected_token
        source_token = normalize_optional_component_source_path_safe(
            (item.get('source_path') or ''),
            'solutions',
            'solution path',
        )
        if source_token and source_token not in expected_by_source:
            expected_by_source[source_token] = expected_token
    expected_by_source_cache: dict[str, str] = dict(expected_by_source)

    def _expected_from_workspace_source(source_rel: str) -> str:
        safe_source = normalize_optional_component_source_path_safe(
            source_rel,
            'solutions',
            'solution path',
        )
        if not safe_source:
            return ''
        cached = normalize_expected_behavior((expected_by_source_cache.get(safe_source) or 'unknown'))
        if cached != 'unknown':
            return cached
        expected_token = 'unknown'
        try:
            entry = solution_metadata_entry(workspace, safe_source)
            expected_token = normalize_expected_behavior((entry.get('expected_behavior') or 'unknown'))
        except Exception:
            expected_token = normalize_expected_behavior(infer_expected_behavior_from_name(safe_source))
        if expected_token != 'unknown':
            expected_by_source_cache[safe_source] = expected_token
            return expected_token
        return ''

    def _collect_build_stage_markers() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], str]:
        if not verification_details:
            return ({}, {}, '')
        stage_summaries: list[dict[str, object]] = []
        generate_stage = verification_stage_summary(verification_details, 'generate_input')
        if generate_stage:
            stage_summaries.append(generate_stage)
        solve_stage = verification_stage_summary(verification_details, 'solve_main')
        if solve_stage:
            stage_summaries.append(solve_stage)
        generate_markers: dict[str, dict[str, str]] = {}
        main_markers: dict[str, dict[str, str]] = {}
        main_source_path = ''

        def _upsert_marker(
            target: dict[str, dict[str, str]],
            *,
            test_name: str,
            updated_at: str,
            short: str,
            kind: str,
            detail: str,
            stage_label: str = '',
        ) -> None:
            safe_test = normalize_run_test_name_token(test_name)
            if not safe_test:
                return
            safe_stamp = (updated_at or '')
            existing = target.get(safe_test)
            if existing is not None:
                existing_stamp = (existing.get('updated_at') or '')
                if existing_stamp and safe_stamp and existing_stamp > safe_stamp:
                    return
            target[safe_test] = {
                'short': short or '--',
                'kind': kind or 'neutral',
                'detail': (detail or ''),
                'updated_at': safe_stamp,
                'stage_label': (stage_label or ''),
            }

        for summary_entry in stage_summaries:
            source_token = (summary_entry.get('verification_source') or '')
            if not source_token:
                source_token = (_verification_source_from_summary(summary_entry) or '')
            marker_target: dict[str, dict[str, str]] | None = None
            run_status = (summary_entry.get('status') or '')
            tests_raw = summary_entry.get('tests')
            if source_token == 'verification.generate-input':
                marker_target = generate_markers
            elif source_token == 'verification.solve-main':
                marker_target = main_markers
                if not main_source_path:
                    source_rel = normalize_workspace_rel_path((_run_source_from_summary(summary_entry) or summary_entry.get('source') or ''))
                    if source_rel:
                        main_source_path = source_rel
            else:
                continue
            run_status = run_status
            stamp = (summary_entry.get('updated_at') or '')
            tests = tests_raw or []
            for test_item in tests:
                test_name = (test_item.get('test') or '')
                if not test_name:
                    continue
                verdict_short = run_verdict_short((test_item.get('verdict') or ''))
                verdict_display = verdict_short if verdict_short and verdict_short != '--' else '--'
                if run_status in {'running', 'queued', 'pending'} and verdict_display == '--':
                    verdict_display = '..'
                if verdict_display == 'AC':
                    kind = 'ok'
                elif verdict_display in {'--', '..'}:
                    kind = 'neutral'
                else:
                    kind = 'fail'
                detail = compact_error_text((test_item.get('message') or test_item.get('error') or ''))
                if (not detail) and kind == 'fail':
                    detail = f'verdict {verdict_display}'
                _upsert_marker(
                    marker_target,
                    test_name=test_name,
                    updated_at=stamp,
                    short=verdict_display,
                    kind=kind,
                    detail=detail,
                    stage_label='validated' if (test_item.get('source_kind') or '') == 'manual' else 'generated' if source_token == 'verification.generate-input' else '',
                )
        return (generate_markers, main_markers, main_source_path)

    def _is_solution_column_source(source_value: str) -> bool:
        safe_source = normalize_optional_component_source_path_safe(
            source_value,
            'solutions',
            'solution path',
        )
        return bool(safe_source)

    def _generate_stage_label(source_value: str, verification_source: str) -> str:
        source_token = (verification_source or '')
        if source_token != 'verification.generate-input':
            return ''
        filename = Path((source_value or '')).name
        if filename == 'manual_validate.cpp':
            return 'validated'
        return 'generated'

    def _stage_note_status(*, short: str, kind: str, run_status: str) -> tuple[str, str]:
        short_token = (short or '').upper()
        kind_token = (kind or '')
        run_token = (run_status or '')
        if kind_token == 'ok' or short_token == 'AC':
            return ('ok', 'ok')
        if kind_token == 'fail':
            return ('fail', 'failed')
        if short_token == '..' or run_token == 'running':
            return ('running', 'running')
        if short_token == '--' and run_token in {'queued', 'pending'}:
            return ('pending', 'pending')
        if short_token not in {'', '--'}:
            return ('fail', 'failed')
        return ('pending', 'pending')

    def _upsert_row_stage_note(
        target: dict[str, list[dict[str, str]]],
        *,
        test_name: str,
        label: str,
        short: str,
        kind: str,
        run_status: str,
        detail: str,
        source_label: str,
    ) -> None:
        def _stage_note_text(stage_label: str, stage_status: str) -> str:
            label_token = (stage_label or '')
            status_token = (stage_status or '')
            if label_token == 'generated':
                if status_token == 'running':
                    return '.. generating'
                if status_token == 'failed':
                    return 'generation failed'
                if status_token == 'ok':
                    return 'generated'
                if status_token == 'pending':
                    return '.. generation pending'
            if label_token == 'validated':
                if status_token == 'running':
                    return '.. validating'
                if status_token == 'failed':
                    return 'validation failed'
                if status_token == 'ok':
                    return 'validated'
                if status_token == 'pending':
                    return '.. validation pending'
            return f"{(stage_label or '')} {stage_status}"

        safe_test = normalize_run_test_name_token(test_name)
        if (not safe_test) or (not label):
            return
        tone, status_label = _stage_note_status(short=short, kind=kind, run_status=run_status)
        notes = target.get(safe_test)
        if notes is None:
            notes = []
            target[safe_test] = notes
        stage_key = (label or '')
        note_payload = {
            'stage_key': stage_key,
            'label': (label or ''),
            'status_label': status_label,
            'tone': tone,
            'text': _stage_note_text((label or ''), status_label),
            'detail': (detail or ''),
            'source_label': (source_label or ''),
        }
        for idx, existing in enumerate(notes):
            if (existing.get('stage_key') or '') == stage_key:
                notes[idx] = note_payload
                return
        notes.append(note_payload)

    def _primary_row_stage_note(notes: list[dict[str, str]]) -> dict[str, str]:
        priority = {'fail': 4, 'running': 3, 'pending': 2, 'ok': 1}
        best: dict[str, str] = {}
        best_score = -1
        for note in notes:
            score = int(priority.get((note.get('tone') or ''), 0))
            if score > best_score:
                best = note
                best_score = score
        return dict(best) if best else {}

    def _test_name_cell(
        *,
        actual_test_name: str,
        fallback_name: str,
        is_placeholder: bool,
        notes: list[dict[str, str]],
        has_detail: bool,
    ) -> dict[str, object]:
        primary_note = _primary_row_stage_note(notes)
        tone = (primary_note.get('tone') or '')
        note_text = (primary_note.get('text') or '')
        note_detail = (primary_note.get('detail') or '')
        if tone in {'running', 'pending'}:
            short = note_text
            meta = ''
            if note_text.startswith('.. '):
                short = '..'
                meta = note_text[3:]
            return {
                'kind': 'running' if tone == 'running' else 'neutral',
                'text': '',
                'short': short or '..',
                'meta': meta or ('running' if tone == 'running' else 'pending'),
                'detail': note_detail,
                'clickable': False,
            }
        visible_name = actual_test_name or fallback_name
        kind = 'neutral'
        if tone == 'ok':
            kind = 'ok'
        elif tone == 'fail':
            kind = 'fail'
        elif is_placeholder:
            kind = 'neutral'
        return {
            'kind': kind,
            'text': visible_name,
            'short': '',
            'meta': '',
            'detail': note_detail,
            'clickable': bool(actual_test_name and has_detail and (not is_placeholder)),
        }

    columns: list[dict] = []
    all_tests: set[str] = set()
    row_stage_notes: dict[str, list[dict[str, str]]] = {}
    selected_test_name_hint = normalize_run_test_name_token(detail_test_name) if include_row_details else ''
    domjudge_case_cells_by_run = _run_domjudge_case_cells(selected_ids)
    for run_id in selected_ids:
        row = verification_run_rows.get(run_id)
        status = 'running'
        mode = execute_mode
        created_at = verification_created_at
        finished_at = ''
        artifact_verification_id = ''
        summary: dict[str, object] = {}
        source_label = ''
        if row is not None:
            status = row['status']
            mode = row['mode']
            created_at = row['created_at']
            finished_at = row['finished_at']
            artifact_verification_id = row['artifact_verification_id']
            summary = dict(row['summary'])
            source_label = row['source_label']
        _cap_summary_list(summary, 'tests', _C.RUN_DETAIL_TEST_LIST_LIMIT, 'tests_truncated', 'tests_total', 'tests_limit')
        _cap_summary_list(summary, 'compile_diagnostics', _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT, 'compile_diagnostics_truncated', 'compile_diagnostics_total', 'compile_diagnostics_limit')
        if include_row_details:
            _cap_run_test_feedback_files(summary, _C.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
        compile_diags = summary.get('compile_diagnostics') or []
        if compile_diags:
            normalized_diags = _normalize_diagnostics(compile_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        source = _run_source_from_summary(summary)
        verification_source = _verification_source_from_summary(summary) or (verification_details.get('verification_source') or '')
        is_main_correct_run = _is_main_correct_verification_source(verification_source)
        source_for_display = source or source_label
        title = Path(source_for_display).name if source_for_display else ''
        if not title:
            title = run_id or 'unknown run'
        source_href = ''
        source_rel = normalize_workspace_rel_path(source_for_display)
        if problem_slug and username and source_rel and workspace_rel_file_exists(workspace, source_rel):
            safe_solution = normalize_optional_component_source_path_safe(source_rel, 'solutions', 'solution path')
            if safe_solution:
                source_href = f'/problems/{problem_slug}/{username}/solutions/editor?path={quote_plus(safe_solution)}'
            else:
                source_href = f'/problems/{problem_slug}/{username}/files?path={quote_plus(source_rel)}&src=run'
        expected_behavior = _run_expected_behavior_from_summary(summary, source_for_display)
        if expected_behavior == 'unknown':
            mapped_expected = expected_by_run_id.get(run_id)
            if not mapped_expected and source_rel:
                mapped_expected = expected_by_source.get(source_rel)
            if not mapped_expected and source_rel:
                mapped_expected = _expected_from_workspace_source(source_rel)
            if mapped_expected:
                expected_behavior = mapped_expected
        matched, completed, observed_pass, match_reason = _verification_solution_match(expected_behavior, status, summary)
        required_codes, allowed_codes = _expected_status_rule(expected_behavior)
        expected_display = _status_rule_expected_display(expected_behavior)
        expected_is_ac_only = bool(required_codes == ('AC',) and allowed_codes == ('AC',))
        got_short = run_actual_short(status, summary)
        got_display = run_actual_display(status, summary)
        result_kind = _run_cell_kind(got_short, expected_behavior) if got_short else 'neutral'
        result_tone_class = f'tone-{result_kind}'
        expected_mismatch = bool(completed and (not matched))
        execution_skipped_from_summary = bool(summary.get('execution_skipped'))
        if not execution_skipped_from_summary and (summary.get('failure_stage') or '') == 'build':
            execution_skipped_from_summary = True
        tests_map: dict[str, dict] = {}
        max_time_ms = 0
        max_memory_kb = 0
        has_test_metrics = False
        tests_raw = summary.get('tests') or []
        has_materialized_tests = bool(tests_raw)
        timeout_limit_ms = _run_timeout_ms_from_summary(summary)
        if timeout_limit_ms <= 0:
            timeout_limit_ms = fallback_timeout_ms
        for idx, item in enumerate(tests_raw, start=1):
                test_name = (item.get('test') or idx)
                if not test_name:
                    continue
                if selected_test_name_hint and test_name != selected_test_name_hint:
                    continue
                verdict = (item.get('verdict') or '').upper() or '-'
                verdict_short = run_verdict_short(verdict)
                try:
                    time_ms = int(item.get('time_ms') or 0)
                except Exception:
                    time_ms = 0
                if (verdict or '').upper().startswith('TL') and timeout_limit_ms > 0 and (time_ms > timeout_limit_ms):
                    time_ms = timeout_limit_ms
                try:
                    time_user_ms = int(item.get('time_user_ms', time_ms) or 0)
                except Exception:
                    time_user_ms = time_ms
                if (verdict or '').upper().startswith('TL') and timeout_limit_ms > 0 and (time_user_ms > timeout_limit_ms):
                    time_user_ms = timeout_limit_ms
                try:
                    time_wall_ms = int(item.get('time_wall_ms', time_user_ms) or 0)
                except Exception:
                    time_wall_ms = time_user_ms
                try:
                    memory_kb = int(item.get('memory_kb') or 0)
                except Exception:
                    memory_kb = 0
                memory_mb_text = run_memory_mb_text(memory_kb)
                has_test_metrics = True
                if time_ms > max_time_ms:
                    max_time_ms = time_ms
                if memory_kb > max_memory_kb:
                    max_memory_kb = memory_kb
                detail_payload: dict[str, object] | None = None
                if include_row_details:
                    passes_raw = item.get('passes')
                    test_stem = Path(test_name).stem
                    feedback_display = '-'
                    inline_feedback = preserve_error_text(
                        (item.get('message') or item.get('error') or ''),
                        max_chars=1600,
                        max_lines=24,
                    )
                    feedback_files = item.get('feedback_files') or []
                    feedback_items: list[str] = []
                    for feedback_entry in feedback_files:
                        token = (feedback_entry or '')
                        if token:
                            feedback_items.append(token)
                    if inline_feedback:
                        feedback_display = inline_feedback
                    feedback_total = len(feedback_items)
                    try:
                        feedback_total = max(feedback_total, int(item.get('feedback_files_total') or 0))
                    except Exception:
                        feedback_total = len(feedback_items)
                    feedback_truncated = bool(item.get('feedback_files_truncated'))
                    if feedback_total > len(feedback_items):
                        feedback_truncated = True
                    if feedback_truncated:
                        hidden_count = max(0, feedback_total - len(feedback_items))
                        if hidden_count > 0 and feedback_display != '-':
                            feedback_display = f'{feedback_display} (+{hidden_count} more)' if feedback_display != '-' else f'+{count_label(hidden_count, "file")}'
                    pass_rows: list[dict[str, str]] = []
                    passes = passes_raw or []
                    if passes:
                        for pass_item in passes:
                            pass_verdict = (pass_item.get('verdict') or '').upper() or '-'
                            pass_verdict_short = run_verdict_short(pass_verdict)
                            try:
                                pass_time_user_ms = int(pass_item.get('time_user_ms', pass_item.get('time_ms', 0)) or 0)
                            except Exception:
                                pass_time_user_ms = 0
                            if (pass_verdict or '').upper().startswith('TL') and timeout_limit_ms > 0 and (pass_time_user_ms > timeout_limit_ms):
                                pass_time_user_ms = timeout_limit_ms
                            try:
                                pass_time_wall_ms = int(pass_item.get('time_wall_ms', pass_time_user_ms) or 0)
                            except Exception:
                                pass_time_wall_ms = pass_time_user_ms
                            try:
                                pass_memory_kb = int(pass_item.get('memory_kb') or 0)
                            except Exception:
                                pass_memory_kb = 0
                            pass_feedback = preserve_error_text(
                                (pass_item.get('feedback') or pass_item.get('message') or ''),
                                max_chars=1600,
                                max_lines=24,
                            )
                            row_feedback_display = pass_feedback or feedback_display
                            output_rel = (pass_item.get('output_ref') or '')
                            if (not output_rel) and test_stem:
                                output_rel = f'{test_stem}.out'
                            checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                            feedback_rel = ''
                            if feedback_items:
                                feedback_rel = (feedback_items[0] or '')
                            pass_rows.append({'pass_label': '-', 'verdict_short': pass_verdict_short, 'kind': _run_cell_kind(pass_verdict, expected_behavior), 'time_display': run_cpu_wall_ms_text(pass_time_user_ms, pass_time_wall_ms), 'memory_display': run_memory_mb_text(pass_memory_kb), 'feedback_display': row_feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    if not pass_rows:
                        output_rel = (item.get('output_ref') or '')
                        if (not output_rel) and test_stem:
                            output_rel = f'{test_stem}.out'
                        checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                        feedback_rel = ''
                        if feedback_items:
                            feedback_rel = (feedback_items[0] or '')
                        pass_rows.append({'pass_label': '-', 'verdict_short': verdict_short, 'kind': _run_cell_kind(verdict, expected_behavior), 'time_display': run_cpu_wall_ms_text(time_user_ms, time_wall_ms), 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    final_row = dict(pass_rows[-1]) if pass_rows else {}
                    for candidate in reversed(pass_rows):
                        verdict_token = (candidate.get('verdict_short') or '')
                        if verdict_token and verdict_token not in {'--', '-'}:
                            final_row = dict(candidate)
                            break
                    detail_payload = {'verdict': verdict, 'verdict_short': verdict_short, 'time_display': f'{time_ms}ms', 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'pass_rows': pass_rows, 'final_row': final_row}
                all_tests.add(test_name)
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': verdict_short,
                    'short': verdict_short,
                    'metrics': f'{time_ms}ms/{memory_mb_text}',
                    'kind': _run_cell_kind(verdict, expected_behavior),
                    'detail': detail_payload,
                    'detail_available': True,
                }
        execution_skipped = bool(execution_skipped_from_summary and (not has_materialized_tests))
        execution_skipped_reason = preserve_error_text(
            (summary.get('execution_skipped_reason') or summary.get('error') or ''),
            max_chars=1600,
            max_lines=24,
        )
        if not execution_skipped:
            case_cells = domjudge_case_cells_by_run.get(run_id) or {}
            for test_name, case_cell in case_cells.items():
                if selected_test_name_hint and test_name != selected_test_name_hint:
                    continue
                all_tests.add(test_name)
                current_cell = tests_map.get(test_name)
                current_short = (current_cell.get('short') or '').upper() if current_cell is not None else ''
                current_has_verdict = bool(current_short and current_short not in {'--', '..'})
                if current_has_verdict:
                    continue
                verdict = (case_cell.get('verdict') or '').upper()
                short = (case_cell.get('short') or '..').upper() or '..'
                try:
                    time_ms = max(0, int(case_cell.get('time_ms') or 0))
                except Exception:
                    time_ms = 0
                try:
                    memory_kb = max(0, int(case_cell.get('memory_kb') or 0))
                except Exception:
                    memory_kb = 0
                metrics = (case_cell.get('metrics') or '-') or '-'
                detail_payload = None
                detail_available = False
                if bool(case_cell.get('reported')):
                    test_stem = Path(test_name).stem
                    output_rel = f'{test_stem}.out' if test_stem else ''
                    checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                    try:
                        case_cpu_ms = max(0, int(case_cell.get('cpu_ms') or time_ms))
                    except Exception:
                        case_cpu_ms = time_ms
                    try:
                        case_wall_ms = max(case_cpu_ms, int(case_cell.get('wall_ms') or case_cpu_ms))
                    except Exception:
                        case_wall_ms = case_cpu_ms
                    pass_row = {
                        'pass_label': '-',
                        'verdict_short': short if short else '--',
                        'kind': _run_cell_kind(verdict, expected_behavior),
                        'time_display': run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms),
                        'memory_display': run_memory_mb_text(memory_kb),
                        'feedback_display': '-',
                        'output_rel': output_rel,
                        'checker_log_rel': checker_log_rel,
                        'feedback_rel': '',
                    }
                    detail_payload = {
                        'verdict': verdict or '-',
                        'verdict_short': short if short else '--',
                        'time_display': f'{time_ms}ms',
                        'memory_display': run_memory_mb_text(memory_kb),
                        'feedback_display': '-',
                        'pass_rows': [pass_row],
                        'final_row': dict(pass_row),
                    }
                    detail_available = True
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': short,
                    'short': short,
                    'metrics': metrics,
                    'kind': _run_cell_kind(verdict, expected_behavior) if verdict else 'neutral',
                    'detail': detail_payload,
                    'detail_available': bool(detail_available),
                }
                if bool(case_cell.get('reported')):
                    has_test_metrics = True
                    if time_ms > max_time_ms:
                        max_time_ms = time_ms
                    if memory_kb > max_memory_kb:
                        max_memory_kb = memory_kb
        max_time_display = f'{max_time_ms}ms' if has_test_metrics else '-'
        max_memory_display = run_memory_mb_text(max_memory_kb) if has_test_metrics else '-'
        column_payload = {'id': run_id, 'artifact_verification_id': artifact_verification_id, 'title': title, 'source': source_for_display or '-', 'source_href': source_href, 'verification_source': verification_source, 'is_main_correct_run': bool(is_main_correct_run), 'status': status, 'status_upper': status.upper(), 'mode': mode, 'created_at': created_at, 'finished_at': finished_at, 'summary': summary, 'has_run_row': bool(row is not None), 'tests_map': tests_map, 'compile_log': summary.get('compile_log') or '', 'compile_diagnostics': summary.get('compile_diagnostics') or [], 'compile_diagnostics_truncated': bool(summary.get('compile_diagnostics_truncated')), 'compile_diagnostics_total': int(summary.get('compile_diagnostics_total') or 0), 'compile_diagnostics_limit': int(summary.get('compile_diagnostics_limit') or 0), 'error': summary.get('error') or '', 'error_display': run_error_display(summary.get('error') or ''), 'tests_total': int(summary.get('tests_total') or len(tests_map)), 'tests_truncated': bool(summary.get('tests_truncated')), 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_display': expected_display, 'expected_is_ac_only': bool(expected_is_ac_only), 'got_short': got_short, 'got_display': got_display, 'result_kind': result_kind, 'result_tone_class': result_tone_class, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': (match_reason or ''), 'execution_skipped': bool(execution_skipped), 'execution_skipped_reason': execution_skipped_reason, 'max_time_ms': int(max_time_ms), 'max_time_display': max_time_display, 'max_memory_kb': int(max_memory_kb), 'max_memory_display': max_memory_display}
        hidden_stage_label = _generate_stage_label(source_for_display, verification_source)
        if hidden_stage_label:
            for test_name, cell in tests_map.items():
                _upsert_row_stage_note(
                    row_stage_notes,
                    test_name=test_name,
                    label=hidden_stage_label,
                    short=(cell.get('short') or cell.get('text') or '--'),
                    kind=(cell.get('kind') or 'neutral'),
                    run_status=status,
                    detail=((cell.get('detail') or {}).get('feedback_display') or ''),
                    source_label=source_for_display,
                )
            continue
        if not _is_solution_column_source(source_for_display):
            continue
        columns.append(column_payload)
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    status_summary = _verification_status_summary(columns)
    if verification_details:
        overall_status = verification_details.get('status') or (verification_record['status'] if verification_record is not None else '') or ''
        if overall_status in {'running', 'queued', 'pending'}:
            status_summary = {
                'status': 'running',
                'status_upper': 'RUNNING',
                'is_failed': False,
                'has_running': True,
                'matched_count': int(status_summary.get('matched_count') or 0),
                'total_count': int(status_summary.get('total_count') or len(columns)),
            }
        elif overall_status in {'failed', 'cancelled'}:
            status_summary = {
                'status': 'failed',
                'status_upper': 'FAILED',
                'is_failed': True,
                'has_running': False,
                'matched_count': int(status_summary.get('matched_count') or 0),
                'total_count': int(status_summary.get('total_count') or len(columns)),
            }
    if (not columns) and verification_details:
        fallback_status = verification_details.get('status') or (verification_record['status'] if verification_record is not None else '') or ''
        fallback_total = 0
        for raw_total in (
            verification_details.get('solution_count'),
            len(verification_run_ids(verification_details)),
            len(verification_source_paths(verification_details)),
        ):
            try:
                fallback_total = max(fallback_total, int(raw_total or 0))
            except Exception:
                continue
        if fallback_status in {'running', 'queued', 'pending'}:
            status_summary = {
                'status': 'running',
                'status_upper': 'RUNNING',
                'is_failed': False,
                'has_running': True,
                'matched_count': 0,
                'total_count': fallback_total,
            }
        elif fallback_status in {'failed', 'cancelled'}:
            status_summary = {
                'status': 'failed',
                'status_upper': 'FAILED',
                'is_failed': True,
                'has_running': False,
                'matched_count': 0,
                'total_count': fallback_total,
            }
        elif fallback_status in {'ok', 'pass'}:
            status_summary = {
                'status': 'ok',
                'status_upper': 'OK',
                'is_failed': False,
                'has_running': False,
                'matched_count': fallback_total,
                'total_count': fallback_total,
            }
    detail_verification_sources = {
        (col.get('verification_source') or '')
        for col in columns
        if col.get('verification_source')
    }
    detail_is_main_correct_run = bool(detail_verification_sources) and detail_verification_sources.issubset({'verification.solve-main'})
    if not detail_is_main_correct_run:
        details_source = verification_details.get('source') or ''
        if details_source == 'verification.solve-main':
            detail_is_main_correct_run = True
    if not detail_is_main_correct_run:
        artifact_verification_status_token = verification_details.get('artifact_verification_status') or ''
        has_materialized_summary = any(col.get('summary') for col in columns)
        if (artifact_verification_status_token in {'running', 'queued', 'pending'}) and (not has_materialized_summary):
            detail_is_main_correct_run = True
    safe_verification_hint = normalize_run_id_token(verification_id_hint)
    if (not detail_is_main_correct_run) and safe_verification_hint.startswith('inv-buildsolve-'):
        detail_is_main_correct_run = True
    artifact_verification_id = verification_details.get('artifact_verification_id') or verification_id_hint or ''
    if not is_canonical_artifact_id(artifact_verification_id):
        artifact_verification_id = ''
    if not artifact_verification_id:
        for col in columns:
            candidate_build = (col.get('artifact_verification_id') or '')
            if is_canonical_artifact_id(candidate_build):
                artifact_verification_id = candidate_build
                break
    generate_stage_map: dict[str, dict[str, str]] = {}
    if artifact_verification_id:
        generate_stage_map, _main_stage_map, _main_stage_source = _collect_build_stage_markers()
    for test_name, marker in generate_stage_map.items():
        _upsert_row_stage_note(
            row_stage_notes,
            test_name=test_name,
            label=(marker.get('stage_label') or 'generated'),
            short=(marker.get('short') or '--'),
            kind=(marker.get('kind') or 'neutral'),
            run_status='',
            detail=(marker.get('detail') or ''),
            source_label='',
        )
    all_tests.update(generate_stage_map.keys())
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    known_tests_by_index: dict[int, str] = {}
    for test_name in ordered_tests:
        try:
            test_index = int(Path(test_name).stem)
        except Exception:
            continue
        if test_index > 0 and test_index not in known_tests_by_index:
            known_tests_by_index[test_index] = test_name
    tests_meta_stats = _verification_tests_meta_stats(verification_details)
    try:
        tests_meta_total = max(0, int(tests_meta_stats.get('total') or 0))
    except Exception:
        tests_meta_total = 0
    display_test_total = max(max(known_tests_by_index.keys(), default=0), tests_meta_total)
    row_index_by_test = {name: idx for idx, name in enumerate(ordered_tests, start=1)}
    detail_rows: list[dict] = []
    if not include_row_details:
        row_entries: list[tuple[int, str, str, bool]] = []
        if bool(status_summary['has_running']) and display_test_total > 0:
            for idx in range(1, display_test_total + 1):
                actual_name = (known_tests_by_index.get(idx) or '')
                row_entries.append((idx, actual_name, actual_name or f'test {idx}', not bool(actual_name)))
        else:
            row_entries = [(idx, test_name, test_name, False) for idx, test_name in enumerate(ordered_tests, start=1)]
        for idx, actual_test_name, display_name, is_placeholder in row_entries:
            cells: list[dict] = []
            has_detail = False
            for col in columns:
                cell = col['tests_map'].get(actual_test_name) if actual_test_name else None
                if cell is None:
                    col_status = (col.get('status') or '')
                    missing_running = col_status == 'running'
                    missing_pending = col_status in {'queued', 'pending'}
                    cells.append(
                        {
                            'text': '..' if (missing_running or missing_pending) else '--',
                            'short': '..' if (missing_running or missing_pending) else '--',
                            'metrics': 'running' if missing_running else 'pending' if missing_pending else '-',
                            'kind': 'neutral',
                            'detail': None,
                        }
                    )
                    continue
                if bool(cell.get('detail_available')):
                    has_detail = True
                cells.append(
                    {
                        'text': (cell.get('text') or '--'),
                        'short': (cell.get('short') or cell.get('text') or '--'),
                        'metrics': (cell.get('metrics') or '-'),
                        'kind': (cell.get('kind') or 'neutral'),
                        'detail': None,
                    }
                )
            stage_notes = list(row_stage_notes.get(actual_test_name or display_name) or [])
            test_cell = _test_name_cell(
                actual_test_name=actual_test_name,
                fallback_name=display_name,
                is_placeholder=bool(is_placeholder),
                notes=stage_notes,
                has_detail=bool(has_detail),
            )
            detail_rows.append(
                {
                    'index': idx,
                    'test_name': actual_test_name or display_name,
                    'display_name': display_name,
                    'test_cell': test_cell,
                    'is_placeholder': bool(is_placeholder),
                    'row_id': f'test-detail-{idx}',
                    'cells': cells,
                    'has_detail': bool(has_detail and (not is_placeholder)),
                }
            )
    else:
        selected_test_name = selected_test_name_hint
        target_tests = ordered_tests
        if selected_test_name:
            target_tests = [name for name in ordered_tests if name == selected_test_name]

        def _verification_artifact_preview(verification_id: str, rel_path: str) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_rel_path = (rel_path or '').lstrip('/')
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_verification_id)):
                return _run_detail_preview_unavailable('missing')
            try:
                preview_file = safe_artifact_path(problem_slug, safe_verification_id, safe_rel_path)
            except HTTPException:
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/{username}/artifacts/{safe_verification_id}/{safe_rel_path}'
            return _run_detail_preview_from_path(preview_file, download_href)

        def _run_artifact_preview(run_id: str, rel_path: str) -> dict[str, object]:
            safe_run_id = normalize_run_id_token(run_id)
            safe_rel_path = (rel_path or '').lstrip('/')
            if not problem_slug or not username or (not safe_run_id) or (not safe_rel_path):
                return _run_detail_preview_unavailable('missing')
            if safe_rel_path.startswith("cache://"):
                service = getattr(config, "judgehost_task_service", None)
                if service is None:
                    return _run_detail_preview_unavailable('missing')
                download_href = (
                    f'/problems/{problem_slug}/{username}/runs/{safe_run_id}/artifacts/{quote_plus(safe_rel_path)}'
                )
                try:
                    blob = service.resolve_artifact_blob(safe_rel_path)
                except Exception:
                    blob = None
                if blob is None:
                    return _run_detail_preview_unavailable('missing')
                return _run_detail_preview_from_bytes(blob, download_href)
            try:
                preview_file = safe_run_artifact_path(ctx, safe_run_id, safe_rel_path)
            except HTTPException:
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/{username}/runs/{safe_run_id}/artifacts/{safe_rel_path}'
            return _run_detail_preview_from_path(preview_file, download_href)

        def _workspace_answer_preview(test_name: str) -> dict[str, object]:
            if not problem_slug or not username:
                return _run_detail_preview_unavailable('missing')
            test_stem = Path((test_name or '')).stem
            if not test_stem:
                return _run_detail_preview_unavailable('missing')
            answer_source_rel = f'tests/answers/{test_stem}.ans'
            try:
                preview_file = safe_workspace_path(workspace, answer_source_rel)
            except HTTPException:
                return _run_detail_preview_unavailable('missing')
            if (not preview_file.exists()) or (not preview_file.is_file()) or preview_file.is_symlink():
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/{username}/files/download?path={quote_plus(answer_source_rel)}&src=workspace'
            return _run_detail_preview_from_path(preview_file, download_href)

        for test_name in target_tests:
            row_index = int(row_index_by_test.get(test_name) or 0)
            if row_index <= 0:
                continue
            input_rel = f'tests/{test_name}'
            answer_name = _run_test_answer_name(test_name)
            answer_rel = f'ans/{answer_name}' if answer_name else ''
            input_preview = _run_detail_preview_unavailable('missing')
            answer_preview = _run_detail_preview_unavailable('missing')
            source_answer_preview = _workspace_answer_preview(test_name)
            for col in columns:
                artifact_verification_id = (col.get('artifact_verification_id') or '')
                if not is_canonical_artifact_id(artifact_verification_id):
                    continue
                if not bool(input_preview.get('available')):
                    input_preview = _verification_artifact_preview(artifact_verification_id, input_rel)
                if answer_rel and (not bool(answer_preview.get('available'))):
                    answer_preview = _verification_artifact_preview(artifact_verification_id, answer_rel)
                if bool(input_preview.get('available')) and (not answer_rel or bool(answer_preview.get('available'))):
                    break
            if bool(source_answer_preview.get('available')) and _run_detail_preview_is_noise(answer_preview):
                answer_preview = source_answer_preview
            cells: list[dict] = []
            for col in columns:
                cell = col['tests_map'].get(test_name)
                if cell is None:
                    cells.append({'text': '--', 'short': '--', 'metrics': '-', 'kind': 'neutral', 'detail': None})
                    continue
                detail_raw = cell.get('detail')
                detail_payload = dict(detail_raw) if detail_raw is not None else None
                if detail_payload is not None:
                    pass_rows_payload: list[dict[str, object]] = []
                    pass_rows_raw = detail_payload.get('pass_rows') or []
                    for pass_item in pass_rows_raw:
                        row_payload = dict(pass_item)
                        output_rel = (row_payload.get('output_rel') or '')
                        output_preview = _run_detail_preview_unavailable('missing')
                        if output_rel:
                            output_preview = _run_artifact_preview((col.get('id') or ''), output_rel)
                        row_payload['output_preview'] = output_preview
                        checker_log_rel = (row_payload.get('checker_log_rel') or '')
                        feedback_rel = (row_payload.get('feedback_rel') or '')
                        feedback_preview = _run_detail_preview_unavailable('missing')
                        if feedback_rel:
                            feedback_preview = _run_artifact_preview((col.get('id') or ''), feedback_rel)
                        elif checker_log_rel:
                            feedback_preview = _run_artifact_preview((col.get('id') or ''), checker_log_rel)
                        row_payload['feedback_preview'] = feedback_preview
                        if (row_payload.get('feedback_display') or '-') == '-':
                            if bool(feedback_preview.get('available')):
                                preview_text = (feedback_preview.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                                first_line = ''
                                for raw_line in preview_text.splitlines():
                                    line = (raw_line or '')
                                    if line:
                                        first_line = line
                                        break
                                if first_line:
                                    if len(first_line) > 160:
                                        first_line = first_line[:157].rstrip() + '...'
                                    row_payload['feedback_display'] = first_line
                        pass_rows_payload.append(row_payload)
                    detail_payload['pass_rows'] = pass_rows_payload
                    final_row_raw = detail_payload.get('final_row')
                    final_row_payload = dict(final_row_raw) if final_row_raw is not None else {}
                    if pass_rows_payload:
                        final_row_payload = dict(pass_rows_payload[-1])
                        for candidate in reversed(pass_rows_payload):
                            verdict_token = (candidate.get('verdict_short') or '')
                            if verdict_token and verdict_token not in {'--', '-'}:
                                final_row_payload = dict(candidate)
                                break
                    feedback_token = (final_row_payload.get('feedback_display') or '-')
                    feedback_preview = final_row_payload.get('feedback_preview')
                    if (not feedback_token) or feedback_token == '-' or feedback_token.startswith('feedback_dir/'):
                        if feedback_preview is not None and bool(feedback_preview.get('available')):
                            preview_text = (feedback_preview.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                            first_line = ''
                            for raw_line in preview_text.splitlines():
                                line = (raw_line or '')
                                if line:
                                    first_line = line
                                    break
                            if first_line:
                                if len(first_line) > 160:
                                    first_line = first_line[:157].rstrip() + '...'
                                feedback_token = first_line
                    if not feedback_token or feedback_token.startswith('feedback_dir/'):
                        feedback_token = '-'
                    final_row_payload['feedback_display'] = feedback_token
                    output_preview = final_row_payload.get('output_preview')
                    interactive_mode = (col.get('mode') or '') in {'interactive', 'multi-pass'}
                    if interactive_mode and output_preview is not None:
                        final_row_payload['interactive_transcript'] = _interactive_transcript_preview(output_preview)
                    detail_payload['final_row'] = final_row_payload
                cells.append({'text': (cell['text']), 'short': (cell.get('short') or cell.get('text') or '--'), 'metrics': (cell.get('metrics') or '-'), 'kind': (cell['kind']), 'detail': detail_payload})
            if detail_is_main_correct_run:
                for cell in cells:
                    detail_payload = cell.get('detail') or {}
                    final_row_payload = detail_payload.get('final_row') or {}
                    output_preview = final_row_payload.get('output_preview')
                    if output_preview is not None and bool(output_preview.get('available')):
                        answer_preview = output_preview
                        break
            stage_notes = list(row_stage_notes.get(test_name) or [])
            test_cell = _test_name_cell(
                actual_test_name=test_name,
                fallback_name=test_name,
                is_placeholder=False,
                notes=stage_notes,
                has_detail=any((cell.get('detail') is not None for cell in cells)),
            )
            detail_rows.append(
                {
                    'index': row_index,
                    'test_name': test_name,
                    'display_name': test_name,
                    'test_cell': test_cell,
                    'is_placeholder': False,
                    'row_id': f'test-detail-{row_index}',
                    'input_preview': input_preview,
                    'answer_preview': answer_preview,
                    'cells': cells,
                    'has_detail': any((cell.get('detail') is not None for cell in cells)),
                }
            )
    rejudge_context = _run_rejudge_context_for_entries(columns, workspace)
    rerun_paths = rejudge_context.get('paths') or []
    progress_total = 0
    for col in columns:
        if bool(col.get('execution_skipped')):
            continue
        try:
            progress_total = max(progress_total, int(col.get('tests_total') or 0))
        except Exception:
            continue
    progress_reported = len(ordered_tests)
    progress_placeholder_total = min(progress_total, 24) if bool(status_summary['has_running']) and progress_total > 0 else 0
    last_updated_candidates: list[str] = [(col.get('finished_at') or '') for col in columns]
    last_updated_candidates.extend([(col.get('created_at') or '') for col in columns])
    last_updated_candidates.append((verification_details.get('updated_at') or ''))
    last_updated_candidates.append((verification_details.get('finished_at') or ''))
    if verification_created_at:
        last_updated_candidates.append(verification_created_at)
    last_updated = _latest_iso_timestamp(last_updated_candidates)
    verification_id = verification_id_hint
    lifecycle_cards: list[dict[str, object]] = []
    if (not verification_details) and verification_id:
        detail_snapshot = load_verification_detail_snapshot(problem_id, verification_id)
        detail_details = detail_snapshot.get('details')
        if detail_details is not None:
            verification_details = dict(detail_details)
        if not verification_created_at:
            verification_created_at = (detail_snapshot.get('created_at') or '')
    has_verification_context = bool(
        verification_id
        or verification_details
        or verification_record is not None
    )
    if selected_ids or has_verification_context:
        lifecycle_cards = [
            _build_verification_lifecycle_card(
                problem_slug=(ctx['problem']['slug']),
                problem_id=int(ctx['problem']['id']),
                workspace_id=int(ctx['workspace']['id']),
                actor_user_id=int(ctx['user']['id']),
                verification_id=verification_id,
                verification_details=verification_details,
                columns=columns,
                row_test_stage_states=row_stage_notes,
                test_row_total=display_test_total,
                detail_status=(status_summary['status']),
                detail_running=bool(status_summary['has_running']),
                progress_reported=progress_reported,
                progress_total=progress_total,
                matched_count=int(status_summary['matched_count']),
                match_total=int(status_summary['total_count']),
            )
        ]
    verification_logs: dict[str, object] = {
        'available': False,
        'title': 'Verification',
        'verification_id': '',
        'status': '',
        'error': '',
        'error_display': '',
        'log_rows': [],
        'diagnostics': [],
        'diagnostics_total': 0,
        'diagnostics_truncated': False,
        'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
    }
    artifact_verification_id = verification_details.get('artifact_verification_id') or verification_id_hint or ''
    source_verification_id = artifact_verification_id if is_canonical_artifact_id(artifact_verification_id) else ''
    if source_verification_id and problem_slug and username:
        source_verification_row = config.db.fetch_one(
            "SELECT status,summary_json FROM verifications WHERE id=? AND problem_id=?",
            [source_verification_id, problem_id],
        )
        source_verification_summary = parse_summary_json(source_verification_row['summary_json'], f'verification/{source_verification_id}') if source_verification_row is not None else {}
        artifact_verification_status = source_verification_row['status'] if source_verification_row is not None else (verification_details.get('artifact_verification_status') or '')
        artifact_verification_error = verification_details.get('artifact_verification_error') or ''
        if not artifact_verification_error:
            artifact_verification_error = source_verification_summary.get('error') or ''
        if not artifact_verification_error:
            artifact_verification_error = verification_details.get('error') or ''
        diagnostics_title = 'Verification'
        log_rows: list[dict[str, str]] = []
        for name in ('failure.log', 'compile.log', 'generate.log', 'validate.log', 'solve.log'):
            rel = f'logs/{name}'
            try:
                safe_artifact_path(problem_slug, source_verification_id, rel)
            except HTTPException:
                continue
            log_rows.append(
                {
                    'name': name,
                    'href': f'/problems/{problem_slug}/{username}/artifacts/{source_verification_id}/{rel}',
                }
            )
        diagnostics_rows: list[dict[str, object]] = []
        diagnostics_total = 0
        diagnostics_truncated = False
        raw_diags = source_verification_summary.get('diagnostics') or []
        if raw_diags:
            diagnostics_total = len(raw_diags)
            capped_diags = raw_diags[: _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
            diagnostics_truncated = diagnostics_total > len(capped_diags)
            normalized_diags = _normalize_diagnostics(capped_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
        if not diagnostics_rows:
            for run_id in verification_run_ids(verification_details):
                run_row = verification_run(verification_details, run_id)
                if not run_row:
                    continue
                run_summary = run_row['summary']
                raw_diags = run_summary.get('compile_diagnostics') or []
                if not raw_diags:
                    continue
                diagnostics_total = len(raw_diags)
                capped_diags = raw_diags[: _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
                diagnostics_truncated = diagnostics_total > len(capped_diags)
                normalized_diags = _normalize_diagnostics(capped_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
                diagnostics_title = (
                    (run_row.get('source_label') or '')
                    or (_run_source_from_summary(run_summary) or '')
                    or 'Verification'
                )
                break
        if diagnostics_rows and (
            (not artifact_verification_error)
            or ('/opt/domjudge/judgehost/judgings/' in artifact_verification_error)
        ):
            first_diag = diagnostics_rows[0]
            diag_location = (first_diag.get('location_display') or '')
            diag_message = (first_diag.get('message') or '')
            if diag_location and diag_message:
                artifact_verification_error = f'{diag_location}: {diag_message}'
            elif diag_message:
                artifact_verification_error = diag_message
        artifact_verification_error = preserve_error_text(artifact_verification_error)
        verification_logs = {
            'available': True,
            'title': diagnostics_title,
            'verification_id': source_verification_id,
            'status': artifact_verification_status,
            'error': artifact_verification_error,
            'error_display': run_error_display(artifact_verification_error),
            'log_rows': log_rows,
            'diagnostics': diagnostics_rows,
            'diagnostics_total': diagnostics_total,
            'diagnostics_truncated': diagnostics_truncated,
            'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
        }

    return {
        'verification_id': verification_id,
        'detail_columns': columns,
        'detail_rows': detail_rows,
        'selected_run_ids': selected_ids,
        'rerun_solution_paths': rerun_paths,
        'rerun_solution_query': (rejudge_context.get('query') or ''),
        'rerun_unavailable_reason': (rejudge_context.get('unavailable_reason') or ''),
        'matched_count': int(status_summary['matched_count']),
        'match_total': int(status_summary['total_count']),
        'all_matched': bool(columns) and all((bool(col.get('matched')) for col in columns)),
        'detail_status': (status_summary['status']),
        'detail_status_upper': (status_summary['status_upper']),
        'detail_is_main_correct_run': bool(detail_is_main_correct_run),
        'detail_running': bool(status_summary['has_running']),
        'detail_last_updated': last_updated,
        'detail_progress_total': progress_total,
        'detail_progress_reported': progress_reported,
        'detail_progress_placeholder_total': progress_placeholder_total,
        'detail_lifecycle_cards': lifecycle_cards,
        'detail_verification_logs': verification_logs,
    }

