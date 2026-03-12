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
    _expected_status_rule,
    parse_summary_json,
    solution_metadata_entry,
    _status_rule_expected_display,
    _verification_solution_match,
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
    _run_invocation_status_summary,
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
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from app.service.platform.process import is_canonical_artifact_id
from app.impl.workspace.run_view_lifecycle_builder import _build_verification_lifecycle_card
from app.impl.workspace.run_view_lifecycle_card import (
    _run_domjudge_case_cells,
    _run_verification_details_from_audit,
    _verification_tests_meta_stats,
)
from app.impl.workspace.run_view_list import (
    _effective_run_timeout_ms,
    _latest_iso_timestamp,
    _run_actual_display,
    _run_actual_short,
    _run_cell_kind,
    _run_cpu_wall_ms_text,
    _run_error_display,
    _run_expected_behavior_from_summary,
    _run_invocation_id_from_summary,
    _run_invocation_source_from_summary,
    _run_is_main_correct_invocation_source,
    _run_memory_mb_text,
    run_source_labels_from_audit,
    _run_test_answer_name,
    _run_test_sort_key,
    _run_timeout_ms_from_summary,
    _run_verdict_short,
)

_C = config.constants

def build_run_detail_context(
    ctx: dict,
    run_ids: list[str],
    execute_mode: str,
    *,
    requested_invocation_id: str = '',
    include_row_details: bool = False,
    detail_test_name: str = '',
) -> dict:
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    problem_id = int(ctx['problem']['id'])
    actor_user_id = int(ctx['user']['id'])
    problem_slug = str(ctx.get('problem', {}).get('slug') or '').strip()
    username = str(ctx.get('user', {}).get('username') or '').strip()
    fallback_timeout_ms = 0
    try:
        _payload, general_cfg, _cfg_path = read_problem_config(workspace)
        fallback_timeout_ms = _effective_run_timeout_ms(
            int(general_cfg.get('time_limit_ms') or _C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            mode=general_cfg.get('mode'),
        )
    except Exception:
        fallback_timeout_ms = 0
    selected_ids = [token for token in run_ids if token]
    rows_by_id: dict[str, dict] = {}
    if selected_ids:
        placeholders = ','.join(('?' for _ in selected_ids))
        rows = config.db.fetch_all(f'\n            SELECT id,build_id,mode,status,summary_json,created_at,finished_at\n            FROM runs\n            WHERE workspace_id=? AND id IN ({placeholders})\n            ', [workspace_id, *selected_ids])
        for row in rows:
            run_id = str(row['id'] or '').strip()
            if run_id:
                rows_by_id[run_id] = dict(row)
    audit_source_labels: dict[str, str] = {}
    try:
        audit_source_labels = run_source_labels_from_audit(problem_id, actor_user_id, selected_ids, limit=max(240, len(selected_ids) * 8))
    except Exception:
        audit_source_labels = {}
    invocation_id_hint = normalize_run_id_token(requested_invocation_id)
    verification_audit_row: dict[str, object] = {}
    verification_details: dict[str, object] = {}
    if invocation_id_hint:
        verification_audit_row = _run_verification_details_from_audit(problem_id, actor_user_id, invocation_id_hint)
        details_obj = verification_audit_row.get('details')
        if isinstance(details_obj, dict):
            verification_details = details_obj
    invocation_created_at = str(verification_audit_row.get('created_at') or '').strip() if isinstance(verification_audit_row, dict) else ''
    expected_by_run_id: dict[str, str] = {}
    expected_by_source: dict[str, str] = {}
    solutions_raw = verification_details.get('solutions')
    if isinstance(solutions_raw, list):
        for item in solutions_raw:
            if not isinstance(item, dict):
                continue
            expected_token = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            if expected_token == 'unknown':
                continue
            run_token = normalize_run_id_token(item.get('run_id'))
            if run_token and run_token not in expected_by_run_id:
                expected_by_run_id[run_token] = expected_token
            source_token = normalize_optional_component_source_path_safe(
                str(item.get('source_path') or ''),
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
        cached = normalize_expected_behavior(str(expected_by_source_cache.get(safe_source) or 'unknown'))
        if cached != 'unknown':
            return cached
        expected_token = 'unknown'
        try:
            entry = solution_metadata_entry(workspace, safe_source)
            expected_token = normalize_expected_behavior(str(entry.get('expected_behavior') or 'unknown'))
        except Exception:
            expected_token = normalize_expected_behavior(infer_expected_behavior_from_name(safe_source))
        if expected_token != 'unknown':
            expected_by_source_cache[safe_source] = expected_token
            return expected_token
        return ''

    def _collect_build_stage_markers(build_id_token: str) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], str]:
        safe_build_id = str(build_id_token or '').strip()
        if (not safe_build_id) or (not is_canonical_artifact_id(safe_build_id)):
            return ({}, {}, '')
        run_rows = config.db.fetch_all(
            """
            SELECT id,status,created_at,finished_at,summary_json
            FROM runs
            WHERE problem_id=? AND workspace_id=? AND build_id=?
            ORDER BY created_at DESC
            LIMIT 512
            """,
            [int(problem_id), int(workspace_id), safe_build_id],
        )
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
        ) -> None:
            safe_test = normalize_run_test_name_token(test_name)
            if not safe_test:
                return
            safe_stamp = str(updated_at or '').strip()
            existing = target.get(safe_test)
            if isinstance(existing, dict):
                existing_stamp = str(existing.get('updated_at') or '').strip()
                if existing_stamp and safe_stamp and existing_stamp > safe_stamp:
                    return
            target[safe_test] = {
                'short': str(short or '--').strip() or '--',
                'kind': str(kind or 'neutral').strip() or 'neutral',
                'detail': str(detail or '').strip(),
                'updated_at': safe_stamp,
            }

        for run_row in run_rows:
            run_id = normalize_run_id_token(run_row['id'])
            if not run_id:
                continue
            summary_obj = parse_summary_json(run_row['summary_json'], f'run/stage/{run_id}')
            if not isinstance(summary_obj, dict):
                continue
            source_token = str(_run_invocation_source_from_summary(summary_obj) or '').strip().lower()
            marker_target: dict[str, dict[str, str]] | None = None
            if source_token == 'build.generate-input':
                marker_target = generate_markers
            elif source_token == 'build.solve':
                marker_target = main_markers
                if not main_source_path:
                    source_rel = normalize_workspace_rel_path(str(_run_source_from_summary(summary_obj) or ''))
                    if source_rel:
                        main_source_path = source_rel
            else:
                continue
            run_status = str(run_row['status'] or '').strip().lower()
            stamp = str(run_row['finished_at'] or run_row['created_at'] or '').strip()
            tests_raw = summary_obj.get('tests')
            if not isinstance(tests_raw, list):
                continue
            for test_item in tests_raw:
                if not isinstance(test_item, dict):
                    continue
                test_name = str(test_item.get('test') or '').strip()
                if not test_name:
                    continue
                verdict_short = _run_verdict_short(str(test_item.get('verdict') or ''))
                verdict_display = verdict_short if verdict_short and verdict_short != '--' else '--'
                if run_status in {'running', 'queued', 'pending'} and verdict_display == '--':
                    verdict_display = '..'
                if verdict_display == 'AC':
                    kind = 'ok'
                elif verdict_display in {'--', '..'}:
                    kind = 'neutral'
                else:
                    kind = 'fail'
                detail = compact_error_text(str(test_item.get('message') or test_item.get('error') or ''))
                if (not detail) and kind == 'fail':
                    detail = f'verdict {verdict_display}'
                _upsert_marker(
                    marker_target,
                    test_name=test_name,
                    updated_at=stamp,
                    short=verdict_display,
                    kind=kind,
                    detail=detail,
                )
        return (generate_markers, main_markers, main_source_path)

    columns: list[dict] = []
    all_tests: set[str] = set()
    selected_test_name_hint = normalize_run_test_name_token(detail_test_name) if include_row_details else ''
    domjudge_case_cells_by_run = _run_domjudge_case_cells(selected_ids)
    for run_id in selected_ids:
        row = rows_by_id.get(run_id)
        status = 'running'
        mode = execute_mode
        created_at = invocation_created_at
        finished_at = ''
        build_id = ''
        summary_raw = None
        if row is not None:
            status = str(row.get('status') or '').strip().lower() or status
            mode = str(row.get('mode') or '').strip() or mode
            created_at = row.get('created_at') or created_at
            finished_at = str(row.get('finished_at') or '').strip()
            build_id = str(row.get('build_id') or '').strip()
            summary_raw = row.get('summary_json')
        summary = parse_summary_json(summary_raw, f'run/{run_id}') if summary_raw else None
        if isinstance(summary, dict):
            _cap_summary_list(summary, 'tests', _C.RUN_DETAIL_TEST_LIST_LIMIT, 'tests_truncated', 'tests_total', 'tests_limit')
            _cap_summary_list(summary, 'compile_diagnostics', _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT, 'compile_diagnostics_truncated', 'compile_diagnostics_total', 'compile_diagnostics_limit')
            if include_row_details:
                _cap_run_test_feedback_files(summary, _C.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
            compile_diags = summary.get('compile_diagnostics')
            if isinstance(compile_diags, list):
                normalized_diags = _normalize_diagnostics(compile_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        source = _run_source_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        is_main_correct_run = _run_is_main_correct_invocation_source(invocation_source)
        audit_source_label = str(audit_source_labels.get(run_id) or '').strip()
        source_for_display = source or audit_source_label
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
        _, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
        expected_display = _status_rule_expected_display(expected_behavior)
        expected_is_ac_only = bool(required_codes == ('AC',) and allowed_codes == ('AC',))
        got_short = _run_actual_short(status, summary)
        got_display = _run_actual_display(status, summary)
        expected_mismatch = bool(completed and (not matched))
        execution_skipped_from_summary = False
        if isinstance(summary, dict):
            execution_skipped_from_summary = bool(summary.get('execution_skipped'))
            if not execution_skipped_from_summary and str(summary.get('failure_stage') or '').strip().lower() == 'build':
                execution_skipped_from_summary = True
        tests_map: dict[str, dict] = {}
        max_time_ms = 0
        max_memory_kb = 0
        has_test_metrics = False
        tests_raw = (summary.get('tests') if isinstance(summary, dict) else None) if not execution_skipped_from_summary else None
        timeout_limit_ms = _run_timeout_ms_from_summary(summary)
        if timeout_limit_ms <= 0:
            timeout_limit_ms = fallback_timeout_ms
        if isinstance(tests_raw, list):
            for idx, item in enumerate(tests_raw, start=1):
                if not isinstance(item, dict):
                    continue
                test_name = str(item.get('test') or idx).strip()
                if not test_name:
                    continue
                if selected_test_name_hint and test_name != selected_test_name_hint:
                    continue
                verdict = str(item.get('verdict') or '').strip().upper() or '-'
                verdict_short = _run_verdict_short(verdict)
                try:
                    time_ms = int(item.get('time_ms') or 0)
                except Exception:
                    time_ms = 0
                if str(verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (time_ms > timeout_limit_ms):
                    time_ms = timeout_limit_ms
                try:
                    time_user_ms = int(item.get('time_user_ms', time_ms) or 0)
                except Exception:
                    time_user_ms = time_ms
                if str(verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (time_user_ms > timeout_limit_ms):
                    time_user_ms = timeout_limit_ms
                try:
                    time_wall_ms = int(item.get('time_wall_ms', time_user_ms) or 0)
                except Exception:
                    time_wall_ms = time_user_ms
                try:
                    memory_kb = int(item.get('memory_kb') or 0)
                except Exception:
                    memory_kb = 0
                memory_mb_text = _run_memory_mb_text(memory_kb)
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
                        str(item.get('message') or item.get('error') or ''),
                        max_chars=1600,
                        max_lines=24,
                    )
                    feedback_files_raw = item.get('feedback_files')
                    feedback_items: list[str] = []
                    if isinstance(feedback_files_raw, list):
                        for feedback_entry in feedback_files_raw:
                            token = str(feedback_entry or '').strip()
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
                    if isinstance(passes_raw, list) and passes_raw:
                        for pass_item in passes_raw:
                            if not isinstance(pass_item, dict):
                                continue
                            pass_verdict = str(pass_item.get('verdict') or '').strip().upper() or '-'
                            pass_verdict_short = _run_verdict_short(pass_verdict)
                            try:
                                pass_time_user_ms = int(pass_item.get('time_user_ms', pass_item.get('time_ms', 0)) or 0)
                            except Exception:
                                pass_time_user_ms = 0
                            if str(pass_verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (pass_time_user_ms > timeout_limit_ms):
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
                                str(pass_item.get('feedback') or pass_item.get('message') or ''),
                                max_chars=1600,
                                max_lines=24,
                            )
                            row_feedback_display = pass_feedback or feedback_display
                            output_rel = str(pass_item.get('output_ref') or '').strip()
                            if (not output_rel) and test_stem:
                                output_rel = f'{test_stem}.out'
                            checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                            feedback_rel = ''
                            if feedback_items:
                                feedback_rel = str(feedback_items[0] or '').strip()
                            pass_rows.append({'pass_label': '-', 'verdict_short': pass_verdict_short, 'kind': _run_cell_kind(pass_verdict, expected_behavior), 'time_display': _run_cpu_wall_ms_text(pass_time_user_ms, pass_time_wall_ms), 'memory_display': _run_memory_mb_text(pass_memory_kb), 'feedback_display': row_feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    if not pass_rows:
                        output_rel = str(item.get('output_ref') or '').strip()
                        if (not output_rel) and test_stem:
                            output_rel = f'{test_stem}.out'
                        checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                        feedback_rel = ''
                        if feedback_items:
                            feedback_rel = str(feedback_items[0] or '').strip()
                        pass_rows.append({'pass_label': '-', 'verdict_short': verdict_short, 'kind': _run_cell_kind(verdict, expected_behavior), 'time_display': _run_cpu_wall_ms_text(time_user_ms, time_wall_ms), 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    final_row = dict(pass_rows[-1]) if pass_rows else {}
                    for candidate in reversed(pass_rows):
                        verdict_token = str(candidate.get('verdict_short') or '').strip()
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
        execution_skipped = bool(execution_skipped_from_summary)
        execution_skipped_reason = ''
        if isinstance(summary, dict):
            execution_skipped_reason = preserve_error_text(
                str(summary.get('execution_skipped_reason') or summary.get('error') or ''),
                max_chars=1600,
                max_lines=24,
            )
        if not execution_skipped:
            case_cells = domjudge_case_cells_by_run.get(run_id) or {}
            for test_name, case_cell in case_cells.items():
                if selected_test_name_hint and test_name != selected_test_name_hint:
                    continue
                all_tests.add(test_name)
                current_cell = tests_map.get(test_name) if isinstance(tests_map.get(test_name), dict) else None
                current_short = str(current_cell.get('short') or '').strip().upper() if isinstance(current_cell, dict) else ''
                current_has_verdict = bool(current_short and current_short not in {'--', '..'})
                if current_has_verdict:
                    continue
                verdict = str(case_cell.get('verdict') or '').strip().upper()
                short = str(case_cell.get('short') or '..').strip().upper() or '..'
                try:
                    time_ms = max(0, int(case_cell.get('time_ms') or 0))
                except Exception:
                    time_ms = 0
                try:
                    memory_kb = max(0, int(case_cell.get('memory_kb') or 0))
                except Exception:
                    memory_kb = 0
                metrics = str(case_cell.get('metrics') or '-').strip() or '-'
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
                        'time_display': _run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms),
                        'memory_display': _run_memory_mb_text(memory_kb),
                        'feedback_display': '-',
                        'output_rel': output_rel,
                        'checker_log_rel': checker_log_rel,
                        'feedback_rel': '',
                    }
                    detail_payload = {
                        'verdict': verdict or '-',
                        'verdict_short': short if short else '--',
                        'time_display': f'{time_ms}ms',
                        'memory_display': _run_memory_mb_text(memory_kb),
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
        max_memory_display = _run_memory_mb_text(max_memory_kb) if has_test_metrics else '-'
        columns.append({'id': run_id, 'build_id': build_id, 'title': title, 'source': source_for_display or '-', 'source_href': source_href, 'invocation_source': invocation_source, 'is_main_correct_run': bool(is_main_correct_run), 'status': status, 'status_upper': status.upper(), 'mode': mode, 'created_at': created_at, 'finished_at': finished_at, 'summary': summary, 'has_run_row': bool(row is not None), 'tests_map': tests_map, 'compile_log': str(summary.get('compile_log') or '') if isinstance(summary, dict) else '', 'compile_diagnostics': summary.get('compile_diagnostics') if isinstance(summary, dict) else [], 'compile_diagnostics_truncated': bool(summary.get('compile_diagnostics_truncated')) if isinstance(summary, dict) else False, 'compile_diagnostics_total': int(summary.get('compile_diagnostics_total') or 0) if isinstance(summary, dict) else 0, 'compile_diagnostics_limit': int(summary.get('compile_diagnostics_limit') or 0) if isinstance(summary, dict) else 0, 'error': str(summary.get('error') or '') if isinstance(summary, dict) else '', 'error_display': _run_error_display(str(summary.get('error') or '')) if isinstance(summary, dict) else '', 'tests_total': int(summary.get('tests_total') or len(tests_map)) if isinstance(summary, dict) else len(tests_map), 'tests_truncated': bool(summary.get('tests_truncated')) if isinstance(summary, dict) else False, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_display': expected_display, 'expected_is_ac_only': bool(expected_is_ac_only), 'got_short': got_short, 'got_display': got_display, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': str(match_reason or ''), 'execution_skipped': bool(execution_skipped), 'execution_skipped_reason': execution_skipped_reason, 'max_time_ms': int(max_time_ms), 'max_time_display': max_time_display, 'max_memory_kb': int(max_memory_kb), 'max_memory_display': max_memory_display})
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    status_summary = _run_invocation_status_summary(columns)
    verification_build_id = str(verification_details.get('build_id') or '').strip() if isinstance(verification_details, dict) else ''
    if not is_canonical_artifact_id(verification_build_id):
        verification_build_id = ''
    if not verification_build_id:
        for col in columns:
            candidate_build = str(col.get('build_id') or '').strip()
            if is_canonical_artifact_id(candidate_build):
                verification_build_id = candidate_build
                break
    gen_stage_map: dict[str, dict[str, str]] = {}
    if verification_build_id:
        gen_stage_map, _main_stage_map, _main_stage_source = _collect_build_stage_markers(verification_build_id)
    all_tests.update(gen_stage_map.keys())
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    known_tests_by_index: dict[int, str] = {}
    for test_name in ordered_tests:
        try:
            test_index = int(Path(test_name).stem)
        except Exception:
            continue
        if test_index > 0 and test_index not in known_tests_by_index:
            known_tests_by_index[test_index] = test_name
    tests_meta_stats = _verification_tests_meta_stats(problem_slug, verification_build_id)
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
                actual_name = str(known_tests_by_index.get(idx) or '').strip()
                row_entries.append((idx, actual_name, actual_name or f'test {idx}', not bool(actual_name)))
        else:
            row_entries = [(idx, test_name, test_name, False) for idx, test_name in enumerate(ordered_tests, start=1)]
        for idx, actual_test_name, display_name, is_placeholder in row_entries:
            cells: list[dict] = []
            has_detail = False
            for col in columns:
                cell = col['tests_map'].get(actual_test_name) if actual_test_name else None
                if cell is None:
                    missing_running = bool(status_summary['has_running'])
                    cells.append(
                        {
                            'text': '..' if missing_running else '--',
                            'short': '..' if missing_running else '--',
                            'metrics': 'running' if missing_running else '-',
                            'kind': 'neutral',
                            'detail': None,
                        }
                    )
                    continue
                if bool(cell.get('detail_available')):
                    has_detail = True
                cells.append(
                    {
                        'text': str(cell.get('text') or '--'),
                        'short': str(cell.get('short') or cell.get('text') or '--'),
                        'metrics': str(cell.get('metrics') or '-'),
                        'kind': str(cell.get('kind') or 'neutral'),
                        'detail': None,
                    }
                )
            detail_rows.append(
                {
                    'index': idx,
                    'test_name': actual_test_name or display_name,
                    'display_name': display_name,
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

        def _build_artifact_preview(build_id: str, rel_path: str) -> dict[str, object]:
            safe_build_id = str(build_id or '').strip()
            safe_rel_path = str(rel_path or '').strip().lstrip('/')
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_build_id)):
                return _run_detail_preview_unavailable('missing')
            try:
                preview_file = safe_artifact_path(problem_slug, safe_build_id, safe_rel_path)
            except HTTPException:
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/{safe_rel_path}'
            return _run_detail_preview_from_path(preview_file, download_href)

        def _run_artifact_preview(run_id: str, rel_path: str) -> dict[str, object]:
            safe_run_id = normalize_run_id_token(run_id)
            safe_rel_path = str(rel_path or '').strip().lstrip('/')
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
            test_stem = Path(str(test_name or '').strip()).stem
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
                build_id = str(col.get('build_id') or '').strip()
                if not is_canonical_artifact_id(build_id):
                    continue
                if not bool(input_preview.get('available')):
                    input_preview = _build_artifact_preview(build_id, input_rel)
                if answer_rel and (not bool(answer_preview.get('available'))):
                    answer_preview = _build_artifact_preview(build_id, answer_rel)
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
                detail_raw = cell.get('detail') if isinstance(cell.get('detail'), dict) else None
                detail_payload = dict(detail_raw) if isinstance(detail_raw, dict) else None
                if detail_payload is not None:
                    pass_rows_payload: list[dict[str, object]] = []
                    pass_rows_raw = detail_payload.get('pass_rows')
                    if isinstance(pass_rows_raw, list):
                        for pass_item in pass_rows_raw:
                            if not isinstance(pass_item, dict):
                                continue
                            row_payload = dict(pass_item)
                            output_rel = str(row_payload.get('output_rel') or '').strip()
                            output_preview = _run_detail_preview_unavailable('missing')
                            if output_rel:
                                output_preview = _run_artifact_preview(str(col.get('id') or ''), output_rel)
                            row_payload['output_preview'] = output_preview
                            checker_log_rel = str(row_payload.get('checker_log_rel') or '').strip()
                            feedback_rel = str(row_payload.get('feedback_rel') or '').strip()
                            feedback_preview = _run_detail_preview_unavailable('missing')
                            if feedback_rel:
                                feedback_preview = _run_artifact_preview(str(col.get('id') or ''), feedback_rel)
                            elif checker_log_rel:
                                feedback_preview = _run_artifact_preview(str(col.get('id') or ''), checker_log_rel)
                            row_payload['feedback_preview'] = feedback_preview
                            if str(row_payload.get('feedback_display') or '-').strip() == '-':
                                if bool(feedback_preview.get('available')):
                                    preview_text = str(feedback_preview.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                                    first_line = ''
                                    for raw_line in preview_text.splitlines():
                                        line = str(raw_line or '').strip()
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
                    final_row_payload = dict(final_row_raw) if isinstance(final_row_raw, dict) else {}
                    if pass_rows_payload:
                        final_row_payload = dict(pass_rows_payload[-1])
                        for candidate in reversed(pass_rows_payload):
                            verdict_token = str(candidate.get('verdict_short') or '').strip()
                            if verdict_token and verdict_token not in {'--', '-'}:
                                final_row_payload = dict(candidate)
                                break
                    feedback_token = str(final_row_payload.get('feedback_display') or '-').strip()
                    feedback_preview_obj = final_row_payload.get('feedback_preview')
                    if (not feedback_token) or feedback_token == '-' or feedback_token.startswith('feedback_dir/'):
                        if isinstance(feedback_preview_obj, dict) and bool(feedback_preview_obj.get('available')):
                            preview_text = str(feedback_preview_obj.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                            first_line = ''
                            for raw_line in preview_text.splitlines():
                                line = str(raw_line or '').strip()
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
                    output_preview_obj = final_row_payload.get('output_preview')
                    interactive_mode = str(col.get('mode') or '').strip().lower() in {'interactive', 'multi-pass'}
                    if interactive_mode and isinstance(output_preview_obj, dict):
                        final_row_payload['interactive_transcript'] = _interactive_transcript_preview(output_preview_obj)
                    detail_payload['final_row'] = final_row_payload
                cells.append({'text': str(cell['text']), 'short': str(cell.get('short') or cell.get('text') or '--'), 'metrics': str(cell.get('metrics') or '-'), 'kind': str(cell['kind']), 'detail': detail_payload})
            detail_rows.append(
                {
                    'index': row_index,
                    'test_name': test_name,
                    'display_name': test_name,
                    'is_placeholder': False,
                    'row_id': f'test-detail-{row_index}',
                    'input_preview': input_preview,
                    'answer_preview': answer_preview,
                    'cells': cells,
                    'has_detail': any((cell.get('detail') is not None for cell in cells)),
                }
            )
    detail_invocation_sources = {
        str(col.get('invocation_source') or '').strip().lower()
        for col in columns
        if isinstance(col, dict) and str(col.get('invocation_source') or '').strip()
    }
    detail_is_main_correct_run = bool(detail_invocation_sources) and detail_invocation_sources.issubset({'build.solve'})
    if (not detail_is_main_correct_run) and isinstance(verification_details, dict):
        details_source = str(verification_details.get('source') or '').strip().lower()
        if details_source == 'build.solve':
            detail_is_main_correct_run = True
    if (not detail_is_main_correct_run) and isinstance(verification_details, dict):
        build_status_token = str(verification_details.get('build_status') or '').strip().lower()
        has_materialized_summary = any(
            isinstance(col, dict) and isinstance(col.get('summary'), dict)
            for col in columns
        )
        if (build_status_token in {'running', 'queued', 'pending'}) and (not has_materialized_summary):
            detail_is_main_correct_run = True
    safe_invocation_hint = normalize_run_id_token(invocation_id_hint)
    if (not detail_is_main_correct_run) and safe_invocation_hint.startswith('inv-buildsolve-'):
        detail_is_main_correct_run = True
    rejudge_context = _run_rejudge_context_for_entries(columns, workspace)
    rerun_paths = rejudge_context.get('paths')
    if not isinstance(rerun_paths, list):
        rerun_paths = []
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
    last_updated_candidates: list[str] = [str(col.get('finished_at') or '').strip() for col in columns]
    last_updated_candidates.extend([str(col.get('created_at') or '').strip() for col in columns])
    if invocation_created_at:
        last_updated_candidates.append(invocation_created_at)
    last_updated = _latest_iso_timestamp(last_updated_candidates)
    invocation_id = invocation_id_hint
    for col in columns:
        summary_obj = col.get('summary')
        token = _run_invocation_id_from_summary(summary_obj if isinstance(summary_obj, dict) else None, '')
        if token:
            invocation_id = token
            break
    lifecycle_cards: list[dict[str, object]] = []
    if (not verification_details) and invocation_id:
        verification_audit_row = _run_verification_details_from_audit(problem_id, actor_user_id, invocation_id)
        details_obj = verification_audit_row.get('details')
        verification_details = details_obj if isinstance(details_obj, dict) else {}
    if selected_ids:
        lifecycle_cards = [
            _build_verification_lifecycle_card(
                problem_slug=str(ctx['problem']['slug']),
                problem_id=int(ctx['problem']['id']),
                workspace_id=int(ctx['workspace']['id']),
                actor_user_id=int(ctx['user']['id']),
                invocation_id=invocation_id,
                verification_details=verification_details,
                columns=columns,
                detail_status=str(status_summary['status']),
                detail_running=bool(status_summary['has_running']),
                progress_reported=progress_reported,
                progress_total=progress_total,
                matched_count=int(status_summary['matched_count']),
                match_total=int(status_summary['total_count']),
            )
        ]
    verification_build: dict[str, object] = {
        'available': False,
        'build_id': '',
        'status': '',
        'error': '',
        'error_display': '',
        'log_rows': [],
        'diagnostics': [],
        'diagnostics_total': 0,
        'diagnostics_truncated': False,
        'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
    }
    try:
        build_id = str(verification_details.get('build_id') or '').strip() if isinstance(verification_details, dict) else ''
        safe_build_id = build_id if is_canonical_artifact_id(build_id) else ''
        if safe_build_id and problem_slug and username:
            build_row = config.db.fetch_one(
                'SELECT status,summary_json FROM builds WHERE id=? AND problem_id=?',
                [safe_build_id, problem_id],
            )
            build_summary = parse_summary_json(build_row['summary_json'], f'build/{safe_build_id}') if build_row is not None else {}
            build_status = str(build_row['status'] or '').strip().lower() if build_row is not None else str(verification_details.get('build_status') or '').strip().lower()
            build_error = str(verification_details.get('build_error') or '').strip()
            if not build_error and isinstance(build_summary, dict):
                build_error = str(build_summary.get('error') or '').strip()
            if not build_error:
                build_error = str(verification_details.get('error') or '').strip()
            log_rows: list[dict[str, str]] = []
            for name in ('failure.log', 'compile.log', 'generate.log', 'validate.log', 'solve.log'):
                rel = f'logs/{name}'
                try:
                    safe_artifact_path(problem_slug, safe_build_id, rel)
                except HTTPException:
                    continue
                log_rows.append(
                    {
                        'name': name,
                        'href': f'/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/{rel}',
                    }
                )
            diagnostics_rows: list[dict[str, object]] = []
            diagnostics_total = 0
            diagnostics_truncated = False
            if isinstance(build_summary, dict):
                raw_diags = build_summary.get('diagnostics')
                if isinstance(raw_diags, list):
                    diagnostics_total = len(raw_diags)
                    capped_diags = raw_diags[: _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
                    diagnostics_truncated = diagnostics_total > len(capped_diags)
                    normalized_diags = _normalize_diagnostics(capped_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                    diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
            verification_build = {
                'available': True,
                'build_id': safe_build_id,
                'status': build_status,
                'error': build_error,
                'error_display': _run_error_display(build_error),
                'log_rows': log_rows,
                'diagnostics': diagnostics_rows,
                'diagnostics_total': diagnostics_total,
                'diagnostics_truncated': diagnostics_truncated,
                'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
            }
    except Exception:
        pass

    return {
        'detail_columns': columns,
        'detail_rows': detail_rows,
        'selected_run_ids': selected_ids,
        'rerun_solution_paths': rerun_paths,
        'rerun_solution_query': str(rejudge_context.get('query') or ''),
        'rerun_unavailable_reason': str(rejudge_context.get('unavailable_reason') or ''),
        'matched_count': int(status_summary['matched_count']),
        'match_total': int(status_summary['total_count']),
        'all_matched': bool(columns) and all((bool(col.get('matched')) for col in columns)),
        'detail_status': str(status_summary['status']),
        'detail_status_upper': str(status_summary['status_upper']),
        'detail_is_main_correct_run': bool(detail_is_main_correct_run),
        'detail_running': bool(status_summary['has_running']),
        'detail_last_updated': last_updated,
        'detail_progress_total': progress_total,
        'detail_progress_reported': progress_reported,
        'detail_progress_placeholder_total': progress_placeholder_total,
        'detail_lifecycle_cards': lifecycle_cards,
        'detail_verification_build': verification_build,
    }


