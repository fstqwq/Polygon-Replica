from __future__ import annotations
import json
import re
from pathlib import Path
from typing import cast
from urllib.parse import quote_plus
from app.impl.runtime.config import config
from .artifact import verification_artifact_blob, verification_blob_virtual_rel
from .context import count_label
from .problem_config import read_problem_config
from .context_operation import (
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
    _run_detail_preview_unavailable,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
    _run_source_from_summary,
)
from app.service.platform.error_text import (
    bounded_display_text,
)
from app.service.platform.workspace_path import (
    normalize_optional_component_source_path_safe,
    normalize_workspace_rel_path,
)
from app.service.verification.runtime import (
    effective_run_timeout_ms,
)
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.platform.process import is_canonical_artifact_id
from app.impl.workspace.run_view_lifecycle_card import (
    load_verification_detail_summary,
    _verification_tests_meta_stats,
)
from app.impl.workspace.context_verification import (
    _expected_status_rule,
    _status_rule_expected_display,
    _verification_solution_match,
    _verification_solution_failure_hint,
)
from app.impl.workspace.run_view_list import (
    _latest_iso_timestamp,
    _run_cell_kind,
    _run_expected_behavior_from_summary,
    _run_task_kind_from_summary,
    _is_main_correct_task_kind,
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
_TASK_KIND_GENERATE_INPUT = "generate-input"
_TASK_KIND_MAIN_CORRECT = "main-correct"
_TASK_KIND_SOLUTION_RUN = "solution-run"
_TRANSIENT_REASON_TOKENS = {"running", "queued", "pending"}
_SANITY_STATUS_TOKENS = {"ok", "passed", "pending", "running", "warning", "failed", "skipped"}
_SANITY_CHECK_ORDER = (
    "empty_output_stability",
    "unicode_output_stability",
    "custom_sample_output",
    "boundary_coverage",
)
_SANITY_CHECK_LABELS = {
    "empty_output_stability": "Empty output stability",
    "unicode_output_stability": "Unicode output stability",
    "custom_sample_output": "Custom sample output",
    "boundary_coverage": "Boundary coverage",
}


def _run_cell_text_tone(verdict: str, expected_behavior: str) -> str:
    short = run_verdict_short(verdict)
    if short == "AC" and expected_behavior not in {"accepted", "unknown"}:
        return "info"
    return ""


def _sanity_checks_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item or "") for item in raw if str(item or "")]


def _sanity_check_label(check_name: str) -> str:
    token = str(check_name or "")
    return _SANITY_CHECK_LABELS.get(token, token.replace("_", " ") or "Sanity check")


def _ordered_sanity_checks(checks: list[str]) -> list[str]:
    check_set = set(checks)
    ordered = [check for check in _SANITY_CHECK_ORDER if check in check_set]
    ordered.extend([check for check in checks if check not in set(ordered)])
    return ordered


def _build_sanity_payload(verification_details: dict[str, object]) -> dict[str, object]:
    sanity_status = str(verification_details.get("sanity_status") or "").strip().lower()
    if sanity_status not in _SANITY_STATUS_TOKENS:
        sanity_status = "unknown"
    validation_status = str(verification_details.get("validation_status") or "").strip().lower()
    if validation_status not in _SANITY_STATUS_TOKENS:
        validation_status = "unknown"
    return {
        "sanity_status": sanity_status,
        "sanity_checked_count": int(verification_details.get("sanity_checked_count") or 0),
        "sanity_checks": _sanity_checks_list(verification_details.get("sanity_checks")),
        "validation_status": validation_status,
        "validated_count": int(verification_details.get("validated_count") or 0),
        "failed_step": str(verification_details.get("failed_step") or ""),
        "failed_check": str(verification_details.get("failed_check") or ""),
        "failed_test": str(verification_details.get("failed_test") or ""),
        "error": str(verification_details.get("error") or ""),
    }


def _sanity_status_tone(status: str) -> str:
    if status == "passed":
        return "ok"
    if status in {"warning", "failed"}:
        return "warn"
    if status in {"pending", "running"}:
        return "info"
    return "muted"


def _sanity_reason(payload: dict[str, object]) -> str:
    error = bounded_display_text(str(payload.get("error") or ""))
    if error:
        return error
    status = str(payload.get("sanity_status") or "")
    failed_check = str(payload.get("failed_check") or "")
    if status == "warning" and failed_check:
        return f"{_sanity_check_label(failed_check)} has warning"
    if status == "failed" and failed_check:
        return f"{_sanity_check_label(failed_check)} failed"
    return ""


def _sanity_task_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    status = str(payload.get("sanity_status") or "")
    failed_check = str(payload.get("failed_check") or "")
    failed_test = str(payload.get("failed_test") or "")
    reason = _sanity_reason(payload)
    rows: list[dict[str, object]] = []
    reached_failure = False
    for check in _ordered_sanity_checks(cast(list[str], payload.get("sanity_checks") or [])):
        row_status = "passed"
        detail = "completed"
        if status in {"pending", "running"}:
            row_status = status
            detail = "waiting for sanity checks" if status == "pending" else "running"
        elif status == "skipped":
            row_status = "skipped"
            detail = "not run for partial verification"
        elif status in {"warning", "failed"}:
            if check == failed_check:
                row_status = status
                detail = reason
                if failed_test and failed_test not in detail:
                    detail = f"{detail} ({failed_test})" if detail else failed_test
                reached_failure = True
            elif reached_failure:
                row_status = "skipped"
                detail = "not reached"
        rows.append(
            {
                "name": check,
                "label": _sanity_check_label(check),
                "status": row_status,
                "tone": _sanity_status_tone(row_status),
                "detail": bounded_display_text(detail),
            }
        )
    return rows


def _detail_sanity_context(verification_id: str, verification_details: dict[str, object]) -> dict[str, object]:
    if not verification_id:
        return {
            "available": False,
            "status": "unknown",
            "reason": "",
            "tasks": [],
            "task_count": 0,
            "ran_count": 0,
            "checked_count": 0,
        }
    payload = _build_sanity_payload(verification_details)
    tasks = _sanity_task_rows(payload)
    return {
        "available": True,
        "status": str(payload["sanity_status"]),
        "reason": _sanity_reason(payload),
        "tasks": tasks,
        "task_count": len(tasks),
        "ran_count": sum(1 for task in tasks if str(task.get("status") or "") in {"passed", "warning", "failed"}),
        "checked_count": int(payload["sanity_checked_count"]),
    }


def _rewrite_failure_reason_with_source(current_reason: str, columns: list[dict[str, object]]) -> str:
    source_reason = ""
    generic_match_reasons: set[str] = set()
    generic_error_texts: set[str] = set()
    for col in columns:
        source_path = str(col.get("source") or "")
        match_reason = str(col.get("match_reason") or "")
        error_text = str(col.get("error") or "")
        if match_reason:
            generic_match_reasons.add(match_reason.strip())
        if error_text:
            generic_error_texts.add(error_text.strip())
        if not (match_reason or error_text):
            continue
        if (not error_text) and match_reason in _TRANSIENT_REASON_TOKENS:
            continue
        reason = _verification_solution_failure_hint(source_path, match_reason, error_text)
        if reason:
            source_reason = reason
            break
    if not source_reason:
        return current_reason
    if not current_reason:
        return source_reason
    if current_reason in generic_match_reasons or current_reason in generic_error_texts:
        return source_reason
    if current_reason in {"verification failed", "solution run did not complete", "verification mismatch"}:
        return source_reason
    if current_reason.startswith("required=[") and ", allowed=[" in current_reason and ", got=[" in current_reason:
        return source_reason
    return current_reason


def _generation_status_text(status: str, verdict: str) -> str:
    status_token = str(status or "")
    verdict_token = str(verdict or "").upper()
    if status_token == VerificationTaskStore.TASK_LEASED:
        return "running"
    if status_token in {VerificationTaskStore.TASK_QUEUED, VerificationTaskStore.TASK_PENDING}:
        return "pending"
    if status_token == VerificationTaskStore.TASK_CANCELLED:
        return "cancelled"
    if verdict_token in {"OK", "AC"}:
        return "OK"
    if verdict_token == "WA":
        return "validation failed"
    if verdict_token.startswith("TL"):
        return "generator TL"
    if verdict_token == "RE":
        return "generator RE"
    if verdict_token in {"CE", "COMPILE_ERROR", "COMPILE ERROR"}:
        return "generator CE"
    if verdict_token in {"FL", "FAIL", "FAILED"}:
        return "validator failed"
    if status_token == VerificationTaskStore.TASK_DONE:
        return "OK"
    if status_token == VerificationTaskStore.TASK_FAILED:
        return verdict_token or "FL"
    return status_token or "-"


def _generate_detail_from_task_row(
    row: VerificationTaskRow,
    *,
    tests_meta_row: dict[str, object] | None,
) -> dict[str, object]:
    status = str(row['status'] or '')
    verdict = str(row['verdict'] or '')
    if status == VerificationTaskStore.TASK_DONE:
        status_text = _generation_status_text(status, verdict)
        tone = 'ok'
    elif status == VerificationTaskStore.TASK_FAILED:
        status_text = _generation_status_text(status, verdict)
        tone = 'fail'
    elif status == VerificationTaskStore.TASK_LEASED:
        status_text = _generation_status_text(status, verdict)
        tone = 'running'
    elif status in {VerificationTaskStore.TASK_QUEUED, VerificationTaskStore.TASK_PENDING}:
        status_text = _generation_status_text(status, verdict)
        tone = 'neutral'
    elif status == VerificationTaskStore.TASK_CANCELLED:
        status_text = _generation_status_text(status, verdict)
        tone = 'neutral'
    else:
        status_text = _generation_status_text(status, verdict)
        tone = 'neutral'
    runtime_ms = 0 if row['runtime_sec'] is None else max(0, int(round(float(row['runtime_sec']) * 1000.0)))
    memory_kb = 0 if row['memory_kb'] is None else max(0, int(row['memory_kb']))
    meta = dict(tests_meta_row or {})
    source_path = str(row['source_path'] or '') or str(meta.get('source') or '')
    command = str(meta.get('command') or '')
    is_manual_validation = bool(str(meta.get('kind') or '') == 'manual' or source_path == 'manual_validate.cpp')
    display_source = 'manual validation' if is_manual_validation else source_path
    display_command = '' if is_manual_validation else command
    title_suffix = 'manual' if is_manual_validation else (display_command or 'gen')
    return {
        'source_path': source_path,
        'command': command,
        'display_source': display_source,
        'display_command': display_command,
        'title_suffix': title_suffix,
        'status': status,
        'verdict': verdict,
        'runtime_ms': runtime_ms,
        'memory_kb': memory_kb,
        'error_text': bounded_display_text(str(row['error_text'] or '')),
        'feedback_text': bounded_display_text(str(row['feedback_text'] or '')),
        'tone': tone,
        'status_text': status_text,
        'feedback_display': bounded_display_text(str(row['feedback_text'] or '')) or '-',
    }

def build_run_detail_context(
    ctx: dict,
    execute_mode: str,
    *,
    requested_verification_id: str = '',
    include_row_details: bool = False,
    detail_test_name: str = '',
) -> dict:
    def _task_graph_column_key_by_source(rows: list[VerificationTaskRow]) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            source_path = normalize_optional_component_source_path_safe(str(row['source_path'] or ''), 'solutions', 'solution path')
            if not source_path:
                continue
            pair = (task_kind, source_path)
            preferred_key = values.get(pair, '')
            run_token = normalize_run_id_token(str(row['logical_run_id'] or ''))
            if run_token:
                values[pair] = run_token
            elif not preferred_key:
                values[pair] = f'task:{task_kind}:{source_path}'
        return values

    def _task_graph_column_keys_from_task_rows(
        rows: list[VerificationTaskRow],
        key_by_source: dict[tuple[str, str], str],
    ) -> list[str]:
        values: list[str] = []
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            source_path = normalize_optional_component_source_path_safe(str(row['source_path'] or ''), 'solutions', 'solution path')
            if not source_path:
                continue
            key = key_by_source.get((task_kind, source_path), '')
            if key and key not in values:
                values.append(key)
        return values

    def _logical_run_ids_from_task_rows(rows: list[VerificationTaskRow]) -> list[str]:
        key_by_source = _task_graph_column_key_by_source(rows)
        return _task_graph_column_keys_from_task_rows(rows, key_by_source)

    def _task_counts_from_rows(rows: list[VerificationTaskRow]) -> dict[str, object]:
        counts = {
            'total': 0,
            'pending': 0,
            'queued': 0,
            'running': 0,
            'done': 0,
            'failed': 0,
            'cancelled': 0,
            'by_kind': {},
        }
        for row in rows:
            status = str(row['status'] or '')
            display_status = 'running' if status == VerificationTaskStore.TASK_LEASED else status
            counts['total'] = int(counts['total']) + 1
            if display_status in counts:
                counts[display_status] = int(counts[display_status]) + 1
        return counts

    def _running_tasks_from_rows(rows: list[VerificationTaskRow]) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        for row in rows:
            if str(row['status'] or '') != VerificationTaskStore.TASK_LEASED:
                continue
            source_path = str(row['source_path'] or '')
            source_label = Path(source_path).name if source_path else '-'
            test_name = str(row['test_name'] or '')
            task_kind = str(row['task_kind'] or '')
            kind_label = task_kind.replace('-', ' ').title()
            label = ' / '.join(token for token in [kind_label, source_label, test_name] if token)
            values.append(
                {
                    'task_id': str(row['id'] or ''),
                    'label': label,
                }
            )
        return values

    def _task_graph_task_status_by_run_and_test(
        rows: list[VerificationTaskRow],
        key_by_source: dict[tuple[str, str], str],
    ) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            source_path = normalize_optional_component_source_path_safe(str(row['source_path'] or ''), 'solutions', 'solution path')
            if not source_path:
                continue
            logical_run_id = key_by_source.get((task_kind, source_path), '')
            test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
            if (not logical_run_id) or (not test_name):
                continue
            values[(logical_run_id, test_name)] = str(row['status'] or '')
        return values

    def _task_graph_fallback_rows(
        rows: list[VerificationTaskRow],
        key_by_source: dict[tuple[str, str], str],
        *,
        verification_record: dict[str, str] | None,
        verification_details: dict[str, object],
        execute_mode: str,
    ) -> dict[str, dict[str, object]]:
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            source_path = normalize_optional_component_source_path_safe(str(row['source_path'] or ''), 'solutions', 'solution path')
            if not source_path:
                continue
            key = key_by_source.get((task_kind, source_path), '')
            if not key:
                continue
            item = grouped.setdefault(
                key,
                {
                    'source_path': source_path,
                    'task_kind': task_kind,
                    'expected_behavior': str(row['expected_behavior'] or ''),
                    'statuses': [],
                    'tests_total': 0,
                    'rows': [],
                },
            )
            cast(list[str], item['statuses']).append(str(row['status'] or ''))
            item['tests_total'] = int(item['tests_total']) + 1
            cast(list[VerificationTaskRow], item['rows']).append(row)
        values: dict[str, dict[str, object]] = {}
        verification_status = str(verification_details.get('status') or (verification_record['status'] if verification_record is not None else '') or '')
        verification_error = str(verification_details.get('error') or verification_record['fail_reason'] if verification_record is not None else '')
        for key, item in grouped.items():
            statuses = list(cast(list[str], item['statuses']))
            grouped_rows = sorted(
                cast(list[VerificationTaskRow], item['rows']),
                key=lambda current: (_run_test_sort_key(str(current['test_name'] or '')), str(current['id'] or '')),
            )
            if VerificationTaskStore.TASK_LEASED in statuses:
                status = 'running'
            elif VerificationTaskStore.TASK_QUEUED in statuses:
                status = 'queued'
            elif VerificationTaskStore.TASK_PENDING in statuses:
                status = 'pending'
            elif VerificationTaskStore.TASK_FAILED in statuses:
                status = 'failed'
            elif VerificationTaskStore.TASK_CANCELLED in statuses:
                status = 'cancelled'
            elif grouped_rows and all(str(current['status'] or '') == VerificationTaskStore.TASK_DONE for current in grouped_rows):
                status = 'ok'
            else:
                status = 'pending'
            tests: list[dict[str, object]] = []
            compile_log = ''
            compile_diagnostics: list[dict[str, object]] = []
            error_text = ''
            max_time_ms = 0
            max_memory_kb = 0
            for row in grouped_rows:
                row_status = str(row['status'] or '')
                if row_status in {VerificationTaskStore.TASK_DONE, VerificationTaskStore.TASK_FAILED}:
                    runtime_ms = 0 if row['runtime_sec'] is None else max(0, int(round(float(row['runtime_sec']) * 1000.0)))
                    cpu_ms = runtime_ms if row['cpu_sec'] is None else max(0, int(round(float(row['cpu_sec']) * 1000.0)))
                    wall_ms = cpu_ms if row['wall_sec'] is None else max(0, int(round(float(row['wall_sec']) * 1000.0)))
                    memory_kb = 0 if row['memory_kb'] is None else max(0, int(row['memory_kb']))
                    feedback_text = str(row['feedback_text'] or row['error_text'] or '')
                    tests.append(
                        {
                            'task_id': str(row['id'] or ''),
                            'test': str(row['test_name'] or ''),
                            'verdict': str(row['verdict'] or '--'),
                            'time_ms': runtime_ms,
                            'time_user_ms': cpu_ms,
                            'time_wall_ms': wall_ms,
                            'memory_kb': memory_kb,
                            'message': feedback_text,
                            'output_ref': str(row['output_ref'] or ''),
                            'feedback_files': [],
                        }
                    )
                    if cpu_ms > max_time_ms:
                        max_time_ms = cpu_ms
                    if memory_kb > max_memory_kb:
                        max_memory_kb = memory_kb
                if (not compile_log) and str(row['compile_log'] or ''):
                    compile_log = str(row['compile_log'] or '')
                diagnostics_json = str(row['diagnostics_json'] or '[]')
                try:
                    task_diagnostics = cast(list[dict[str, object]], json.loads(diagnostics_json))
                except Exception:
                    task_diagnostics = []
                if task_diagnostics:
                    compile_diagnostics.extend(task_diagnostics)
                if (not error_text) and str(row['error_text'] or ''):
                    error_text = str(row['error_text'] or '')
            summary: dict[str, object] = {
                'mode': execute_mode,
                'source': str(item['source_path']),
                'task_kind': str(item['task_kind']),
                'expected_behavior': str(item['expected_behavior']),
                'tests_total': int(item['tests_total']),
                'tests': tests,
                'compile_log': compile_log,
                'compile_diagnostics': compile_diagnostics,
                'error': error_text,
                'usage': {
                    'tests': len(tests),
                    'time_ms_total': max_time_ms,
                    'time_user_ms_total': max_time_ms,
                    'time_wall_ms_total': max_time_ms,
                    'memory_kb_peak': max_memory_kb,
                },
            }
            if VerificationTaskStore.TASK_CANCELLED in statuses:
                summary['cancelled'] = True
                if verification_status == 'failed' and verification_error and (not summary['error']):
                    summary['error'] = verification_error
            values[key] = {
                'id': key,
                'artifact_verification_id': verification_details.get('artifact_verification_id') or requested_verification_id or '',
                'mode': execute_mode,
                'status': status,
                'source_label': str(item['source_path']),
                'summary': summary,
                'created_at': verification_record['created_at'] if verification_record is not None else '',
                'finished_at': _latest_iso_timestamp([str(current['finished_at'] or '') for current in grouped_rows]) or (verification_record['finished_at'] if verification_record is not None else ''),
            }
        return values

    def _missing_solution_cell(task_status: str) -> dict[str, object]:
        if task_status == VerificationTaskStore.TASK_LEASED:
            return {
                'text': '..',
                'short': '..',
                'metrics': 'running',
                'kind': 'running',
                'text_tone': '',
                'detail': None,
            }
        if task_status == VerificationTaskStore.TASK_FAILED:
            return {
                'text': 'FL',
                'short': 'FL',
                'metrics': 'failed',
                'kind': 'fail',
                'text_tone': '',
                'detail': None,
            }
        if task_status == VerificationTaskStore.TASK_CANCELLED:
            return {
                'text': '--',
                'short': '--',
                'metrics': 'cancelled',
                'kind': 'neutral',
                'text_tone': '',
                'detail': None,
            }
        return {
            'text': '..',
            'short': '..',
            'metrics': '',
            'kind': 'neutral',
            'text_tone': '',
            'detail': None,
        }

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
            pass_limit=general_cfg.get('pass_limit'),
            default_ms=int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            min_ms=int(_C.GENERAL_TIME_LIMIT_MIN_MS),
            max_ms=int(_C.GENERAL_TIME_LIMIT_MAX_MS),
            pass_fail_slack_sec=int(_C.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC),
            multi_pass_slack_sec=int(_C.RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC),
            interactive_slack_sec=int(_C.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC),
        )
    except Exception:
        fallback_timeout_ms = 0
    selected_ids: list[str] = []
    verification_run_rows: dict[str, dict[str, object]] = {}
    verification_id_hint = normalize_run_id_token(requested_verification_id)
    verification_details: dict[str, object] = {}
    task_rows: list[VerificationTaskRow] = []
    has_task_graph = False
    source_verification_id = verification_id_hint if is_canonical_artifact_id(verification_id_hint) else ''
    verification_record = config.verification_service.verification_record(verification_id_hint) if verification_id_hint else None
    if verification_record is not None and verification_record['workspace_id'] == workspace_id:
        task_rows = VerificationTaskStore(config.db).list_rows(verification_id_hint)
        has_task_graph = bool(task_rows)
        verification_detail_payload = config.verification_service.verification_detail(verification_id_hint)
        verification_details = {
            **verification_detail_payload,
            **config.verification_service.verification_runtime_summary(verification_id_hint),
            'verification_id': verification_id_hint,
            'artifact_verification_id': verification_id_hint,
            'status': verification_record['status'],
            'created_at': verification_record['created_at'],
            'finished_at': verification_record['finished_at'] or '',
        }
        verification_details['task_graph'] = has_task_graph
        source_verification_id = str(verification_details.get('artifact_verification_id') or verification_id_hint or '')
        if not is_canonical_artifact_id(source_verification_id):
            source_verification_id = ''
        if has_task_graph:
            task_graph_key_by_source = _task_graph_column_key_by_source(task_rows)
            verification_run_rows = _task_graph_fallback_rows(
                task_rows,
                task_graph_key_by_source,
                verification_record=verification_record,
                verification_details=verification_details,
                execute_mode=execute_mode,
            )
            if not selected_ids:
                selected_ids.extend(_task_graph_column_keys_from_task_rows(task_rows, task_graph_key_by_source))
        else:
            raw_runs = verification_details.get('runs') if isinstance(verification_details.get('runs'), dict) else {}
            raw_runs_order = verification_details.get('runs_order') if isinstance(verification_details.get('runs_order'), list) else []
            selected_ids = [str(item or '').strip() for item in raw_runs_order if str(item or '').strip()]
            if not selected_ids:
                selected_ids = [str(key or '').strip() for key in raw_runs.keys() if str(key or '').strip()]
            for run_id in selected_ids:
                run_payload = raw_runs.get(run_id)
                if not isinstance(run_payload, dict):
                    continue
                run_summary = dict(run_payload.get('summary') or {})
                verification_run_rows[run_id] = {
                    'id': run_id,
                    'status': str(run_payload.get('status') or verification_details.get('status') or 'running'),
                    'mode': str(run_summary.get('mode') or verification_details.get('mode') or execute_mode),
                    'created_at': str(verification_details.get('created_at') or verification_record['created_at'] if verification_record is not None else ''),
                    'finished_at': str(verification_details.get('finished_at') or verification_record['finished_at'] if verification_record is not None else ''),
                    'artifact_verification_id': str(verification_details.get('artifact_verification_id') or verification_id_hint or ''),
                    'summary': run_summary,
                    'source_label': str(run_payload.get('source_label') or run_summary.get('source') or run_id),
                    'task_kind': str(run_payload.get('task_kind') or run_summary.get('task_kind') or ''),
                }
    verification_created_at = ''
    if not verification_created_at and verification_record is not None:
        verification_created_at = verification_record['created_at']
    solutions = []
    task_graph_key_by_source: dict[tuple[str, str], str] = _task_graph_column_key_by_source(task_rows) if has_task_graph else {}
    preferred_solution_run_ids = _logical_run_ids_from_task_rows(task_rows) if has_task_graph else []
    if preferred_solution_run_ids:
        selected_ids = list(preferred_solution_run_ids)
    expected_by_run_id: dict[str, str] = {}
    expected_by_source: dict[str, str] = {}
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

    def _is_solution_column_source(source_value: str) -> bool:
        safe_source = normalize_optional_component_source_path_safe(
            source_value,
            'solutions',
            'solution path',
        )
        return bool(safe_source)

    def _stage_note_status(*, short: str, kind: str, run_status: str) -> tuple[str, str]:
        short_token = (short or '').upper()
        kind_token = (kind or '')
        run_token = (run_status or '')
        if run_token == 'cancelled':
            return ('neutral', 'cancelled')
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

    def _generate_stage_note_from_rows(rows: list[VerificationTaskRow]) -> dict[str, dict[str, str]]:
        target: dict[str, dict[str, str]] = {}
        for row in rows:
            if str(row['task_kind'] or '') != _TASK_KIND_GENERATE_INPUT:
                continue
            status = str(row['status'] or '')
            if status == VerificationTaskStore.TASK_DONE:
                short = 'AC'
                kind = 'ok'
            elif status == VerificationTaskStore.TASK_FAILED:
                short = 'FL'
                kind = 'fail'
            elif status == VerificationTaskStore.TASK_LEASED:
                short = '..'
                kind = 'neutral'
            else:
                short = '--'
                kind = 'neutral'
            tone, status_label = _stage_note_status(short=short, kind=kind, run_status=status)
            test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
            if not test_name:
                continue
            if status_label == 'running':
                text = '.. generating'
            elif status_label == 'failed':
                text = 'failed'
            elif status_label == 'cancelled':
                text = 'cancelled'
            elif status_label == 'ok':
                text = 'ready'
            else:
                text = ''
            detail = bounded_display_text(str(row['error_text'] or '')) or bounded_display_text(str(row['feedback_text'] or ''))
            target[test_name] = {
                'tone': tone,
                'status_label': status_label,
                'text': text,
                'detail': detail,
            }
        return target

    def _test_name_cell(
        *,
        actual_test_name: str,
        fallback_name: str,
        is_placeholder: bool,
        note: dict[str, str],
        has_detail: bool,
    ) -> dict[str, object]:
        tone = str(note.get('tone') or '')
        note_text = str(note.get('text') or '')
        note_detail = str(note.get('detail') or '')
        if is_placeholder and (not actual_test_name):
            return {
                'kind': 'neutral',
                'text': '',
                'short': '..',
                'meta': 'generating',
                'detail': note_detail,
                'clickable': False,
            }
        if tone in {'running', 'pending'}:
            visible_name = actual_test_name or fallback_name
            meta = note_text.removeprefix('.. ').strip()
            if tone == 'pending':
                meta = ''
            if visible_name:
                return {
                    'kind': 'running' if tone == 'running' else 'neutral',
                    'text': visible_name,
                    'short': '',
                    'meta': meta or ('running' if tone == 'running' else ''),
                    'detail': note_detail,
                    'clickable': False,
                }
            short = note_text
            if note_text.startswith('.. '):
                short = '..'
            if tone == 'pending':
                short = '..'
            return {
                'kind': 'running' if tone == 'running' else 'neutral',
                'text': '',
                'short': short or '..',
                'meta': meta or ('running' if tone == 'running' else ''),
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
    row_generate_notes: dict[str, dict[str, str]] = {}
    task_graph_task_status_by_run_and_test: dict[tuple[str, str], str] = {}
    if has_task_graph:
        row_generate_notes = _generate_stage_note_from_rows(task_rows)
        task_graph_task_status_by_run_and_test = _task_graph_task_status_by_run_and_test(task_rows, task_graph_key_by_source)
        for row in task_rows:
            test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
            if test_name:
                all_tests.add(test_name)
    selected_test_name_hint = normalize_run_test_name_token(detail_test_name) if include_row_details else ''
    domjudge_case_cells_by_run: dict[str, dict[str, dict[str, object]]] = {}
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
        raw_compile_diags = list(cast(list[object], summary.get('compile_diagnostics') or []))
        if raw_compile_diags:
            summary['compile_diagnostics'] = raw_compile_diags[: _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
        if include_row_details:
            _cap_run_test_feedback_files(summary, _C.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
        compile_diags = summary.get('compile_diagnostics') or []
        if compile_diags:
            normalized_diags = _normalize_diagnostics(compile_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        detail_compile_diagnostics = list(cast(list[dict[str, object]], summary.get('compile_diagnostics') or []))
        detail_compile_error = bounded_display_text(summary.get('error') or '')
        if (not detail_compile_error) and detail_compile_diagnostics:
            first_diag = detail_compile_diagnostics[0]
            diag_location = str(first_diag.get('location_display') or '').strip()
            diag_message = str(first_diag.get('message') or '').strip()
            if diag_location and diag_message:
                detail_compile_error = f'{diag_location}: {diag_message}'
            elif diag_message:
                detail_compile_error = diag_message
        source = _run_source_from_summary(summary)
        task_kind = _run_task_kind_from_summary(summary)
        is_main_correct_run = _is_main_correct_task_kind(task_kind)
        source_for_display = source or source_label
        title = Path(source_for_display).name if source_for_display else ''
        if not title:
            title = run_id or 'unknown run'
        source_href = ''
        source_rel = normalize_workspace_rel_path(source_for_display)
        if problem_slug and username and source_rel and workspace_rel_file_exists(workspace, source_rel):
            safe_solution = normalize_optional_component_source_path_safe(source_rel, 'solutions', 'solution path')
            if safe_solution:
                source_href = f'/problems/{problem_slug}/solutions/editor?path={quote_plus(safe_solution)}'
            else:
                source_href = f'/problems/{problem_slug}/files?path={quote_plus(source_rel)}'
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
        result_text_tone = _run_cell_text_tone(got_short, expected_behavior)
        result_tone_class = f'tone-{result_kind}'
        expected_mismatch = bool(expected_is_ac_only and completed and (not matched))
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
                    inline_feedback = bounded_display_text(item.get('message') or item.get('error') or '')
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
                            pass_feedback = bounded_display_text(pass_item.get('feedback') or pass_item.get('message') or '')
                            row_feedback_display = pass_feedback or feedback_display
                            output_rel = (pass_item.get('output_ref') or '')
                            output_task_id = str(pass_item.get('task_id') or '')
                            checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                            feedback_rel = ''
                            if feedback_items:
                                feedback_rel = (feedback_items[0] or '')
                            pass_time_display = run_cpu_wall_ms_text(pass_time_user_ms, pass_time_wall_ms)
                            pass_memory_display = run_memory_mb_text(pass_memory_kb)
                            pass_rows.append({'pass_label': '-', 'verdict_short': pass_verdict_short, 'text_tone': _run_cell_text_tone(pass_verdict, expected_behavior), 'kind': _run_cell_kind(pass_verdict, expected_behavior), 'time_display': pass_time_display, 'memory_display': pass_memory_display, 'status_display': f'{pass_verdict_short} · {pass_time_display} · {pass_memory_display}', 'feedback_display': row_feedback_display, 'output_rel': output_rel, 'output_task_id': output_task_id, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    if not pass_rows:
                        output_rel = (item.get('output_ref') or '')
                        output_task_id = str(item.get('task_id') or '')
                        checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                        feedback_rel = ''
                        if feedback_items:
                            feedback_rel = (feedback_items[0] or '')
                        time_display = run_cpu_wall_ms_text(time_user_ms, time_wall_ms)
                        pass_rows.append({'pass_label': '-', 'verdict_short': verdict_short, 'text_tone': _run_cell_text_tone(verdict, expected_behavior), 'kind': _run_cell_kind(verdict, expected_behavior), 'time_display': time_display, 'memory_display': memory_mb_text, 'status_display': f'{verdict_short} · {time_display} · {memory_mb_text}', 'feedback_display': feedback_display, 'output_rel': output_rel, 'output_task_id': output_task_id, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    final_row = dict(pass_rows[-1]) if pass_rows else {}
                    for candidate in reversed(pass_rows):
                        verdict_token = (candidate.get('verdict_short') or '')
                        if verdict_token and verdict_token not in {'--', '-'}:
                            final_row = dict(candidate)
                            break
                    detail_payload = {
                        'verdict': verdict,
                        'verdict_short': verdict_short,
                        'time_display': f'{time_ms}ms',
                        'memory_display': memory_mb_text,
                        'status_display': f'{verdict_short} · {run_cpu_wall_ms_text(time_user_ms, time_wall_ms)} · {memory_mb_text}',
                        'feedback_display': feedback_display,
                        'pass_rows': pass_rows,
                        'final_row': final_row,
                        'compile_error_display': detail_compile_error,
                        'compile_diagnostics': detail_compile_diagnostics,
                    }
                all_tests.add(test_name)
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': verdict_short,
                    'short': verdict_short,
                    'metrics': f'{time_ms}ms/{memory_mb_text}',
                    'kind': _run_cell_kind(verdict, expected_behavior),
                    'text_tone': _run_cell_text_tone(verdict, expected_behavior),
                    'detail': detail_payload,
                    'detail_available': True,
                }
        execution_skipped = bool(execution_skipped_from_summary and (not has_materialized_tests))
        execution_skipped_reason = bounded_display_text(
            (summary.get('execution_skipped_reason') or summary.get('error') or '')
        )
        if (not has_task_graph) and (not execution_skipped):
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
                        'text_tone': _run_cell_text_tone(verdict, expected_behavior),
                        'kind': _run_cell_kind(verdict, expected_behavior),
                        'time_display': run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms),
                        'memory_display': run_memory_mb_text(memory_kb),
                        'status_display': f'{short if short else "--"} · {run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms)} · {run_memory_mb_text(memory_kb)}',
                        'feedback_display': '-',
                        'output_rel': output_rel,
                        'output_task_id': '',
                        'checker_log_rel': checker_log_rel,
                        'feedback_rel': '',
                    }
                    detail_payload = {
                        'verdict': verdict or '-',
                        'verdict_short': short if short else '--',
                        'time_display': f'{time_ms}ms',
                        'memory_display': run_memory_mb_text(memory_kb),
                        'status_display': f'{short if short else "--"} · {run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms)} · {run_memory_mb_text(memory_kb)}',
                        'feedback_display': '-',
                        'pass_rows': [pass_row],
                        'final_row': dict(pass_row),
                        'compile_error_display': detail_compile_error,
                        'compile_diagnostics': detail_compile_diagnostics,
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
                    'text_tone': _run_cell_text_tone(verdict, expected_behavior) if verdict else '',
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
        column_payload = {'id': run_id, 'artifact_verification_id': artifact_verification_id, 'title': title, 'source': source_for_display or '-', 'source_href': source_href, 'task_kind': task_kind, 'is_main_correct_run': bool(is_main_correct_run), 'status': status, 'mode': mode, 'created_at': created_at, 'finished_at': finished_at, 'summary': summary, 'has_run_row': bool(row is not None), 'tests_map': tests_map, 'compile_log': summary.get('compile_log') or '', 'compile_diagnostics': summary.get('compile_diagnostics') or [], 'error': summary.get('error') or '', 'error_display': run_error_display(summary.get('error') or ''), 'tests_total': int(summary.get('tests_total') or len(tests_map)), 'tests_truncated': bool(summary.get('tests_truncated')), 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_display': expected_display, 'expected_is_ac_only': bool(expected_is_ac_only), 'got_short': got_short, 'got_display': got_display, 'result_kind': result_kind, 'result_text_tone': result_text_tone, 'result_tone_class': result_tone_class, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': (match_reason or ''), 'execution_skipped': bool(execution_skipped), 'execution_skipped_reason': execution_skipped_reason, 'max_time_ms': int(max_time_ms), 'max_time_display': max_time_display, 'max_memory_kb': int(max_memory_kb), 'max_memory_display': max_memory_display}
        if not _is_solution_column_source(source_for_display):
            if include_row_details and task_kind in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                pass
            else:
                continue
        columns.append(column_payload)
    if (not has_task_graph) and columns:
        deduped_columns_by_source: dict[tuple[str, str], dict] = {}
        deduped_order: list[tuple[str, str]] = []
        for col in columns:
            source_key = (
                str(col.get('source') or ''),
                str(col.get('expected_behavior') or ''),
            )
            if source_key not in deduped_columns_by_source:
                deduped_order.append(source_key)
            deduped_columns_by_source[source_key] = col
        columns = [deduped_columns_by_source[key] for key in deduped_order]
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    status_summary = _verification_status_summary(columns)
    if verification_details:
        overall_status = verification_details.get('status') or (verification_record['status'] if verification_record is not None else '') or ''
        if overall_status in {'running', 'queued', 'pending'}:
            status_summary = {
                'status': 'running',
                'is_failed': False,
                'has_running': True,
                'matched_count': int(status_summary.get('matched_count') or 0),
                'total_count': int(status_summary.get('total_count') or len(columns)),
            }
        elif overall_status == 'failed':
            status_summary = {
                'status': 'failed',
                'is_failed': True,
                'has_running': False,
                'matched_count': int(status_summary.get('matched_count') or 0),
                'total_count': int(status_summary.get('total_count') or len(columns)),
            }
    if (not columns) and verification_details:
        fallback_status = verification_details.get('status') or (verification_record['status'] if verification_record is not None else '') or ''
        fallback_total = 0
        raw_totals: list[object] = [len(verification_details.get('source_paths') or [])]
        for raw_total in raw_totals:
            try:
                fallback_total = max(fallback_total, int(raw_total or 0))
            except Exception:
                continue
        if fallback_status in {'running', 'queued', 'pending'}:
            status_summary = {
                'status': 'running',
                'is_failed': False,
                'has_running': True,
                'matched_count': 0,
                'total_count': fallback_total,
            }
        elif fallback_status == 'failed':
            status_summary = {
                'status': 'failed',
                'is_failed': True,
                'has_running': False,
                'matched_count': 0,
                'total_count': fallback_total,
            }
        elif fallback_status in {'ok', 'pass'}:
            status_summary = {
                'status': 'ok',
                'is_failed': False,
                'has_running': False,
                'matched_count': fallback_total,
                'total_count': fallback_total,
            }
    detail_task_kinds = {
        (col.get('task_kind') or '')
        for col in columns
        if col.get('task_kind')
    }
    detail_is_main_correct_run = bool(detail_task_kinds) and detail_task_kinds.issubset({'main-correct'})
    if has_task_graph:
        detail_is_main_correct_run = False
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
    column_tests_total = 0
    for col in columns:
        try:
            column_tests_total = max(column_tests_total, int(col.get('tests_total') or 0))
        except Exception:
            continue
    display_test_total = max(max(known_tests_by_index.keys(), default=0), tests_meta_total, column_tests_total)
    row_index_by_test = {name: idx for idx, name in enumerate(ordered_tests, start=1)}
    tests_meta_by_test_name: dict[str, dict[str, object]] = {}
    for item in cast(list[object], verification_details.get('tests_meta_rows') or []):
        if not isinstance(item, dict):
            continue
        test_name = normalize_run_test_name_token(str(item.get('test_name') or ''))
        if test_name and test_name not in tests_meta_by_test_name:
            tests_meta_by_test_name[test_name] = dict(item)
    generate_task_by_test_name: dict[str, VerificationTaskRow] = {}
    for row in task_rows:
        if str(row['task_kind'] or '') != _TASK_KIND_GENERATE_INPUT:
            continue
        test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
        if test_name:
            generate_task_by_test_name[test_name] = row
    detail_rows: list[dict] = []
    if not include_row_details:
        row_entries: list[tuple[int, str, str, bool]] = []
        if bool(status_summary['has_running']) and display_test_total > 0:
            for idx in range(1, display_test_total + 1):
                actual_name = (known_tests_by_index.get(idx) or '')
                display_name = actual_name or f'{idx:03d}.in'
                row_entries.append((idx, actual_name, display_name, not bool(actual_name)))
        else:
            row_entries = [(idx, test_name, test_name, False) for idx, test_name in enumerate(ordered_tests, start=1)]
        for idx, actual_test_name, display_name, is_placeholder in row_entries:
            cells: list[dict] = []
            has_detail = False
            has_generate_detail = bool(actual_test_name and generate_task_by_test_name.get(actual_test_name))
            for col in columns:
                cell = col['tests_map'].get(actual_test_name) if actual_test_name else None
                if cell is None:
                    if has_task_graph and actual_test_name:
                        task_status = task_graph_task_status_by_run_and_test.get((str(col.get('id') or ''), actual_test_name), '')
                        cells.append(_missing_solution_cell(task_status))
                    else:
                        col_status = (col.get('status') or '')
                        missing_running = col_status == 'running'
                        missing_pending = col_status in {'queued', 'pending'}
                        cells.append(
                            {
                                'text': '..' if (missing_running or missing_pending) else '--',
                                'short': '..' if (missing_running or missing_pending) else '--',
                                'metrics': 'running' if missing_running else '' if missing_pending else '-',
                                'kind': 'running' if missing_running else 'neutral',
                                'text_tone': '',
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
                        'text_tone': (cell.get('text_tone') or ''),
                        'detail': None,
                    }
                )
            generate_note = dict(row_generate_notes.get(actual_test_name or display_name) or {})
            test_cell = _test_name_cell(
                actual_test_name=actual_test_name,
                fallback_name=display_name,
                is_placeholder=bool(is_placeholder),
                note=generate_note,
                has_detail=bool(has_detail or has_generate_detail),
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
                    'has_detail': bool((has_detail or has_generate_detail) and (not is_placeholder)),
                }
            )
    else:
        selected_test_name = selected_test_name_hint
        target_tests = ordered_tests
        if selected_test_name:
            target_tests = [name for name in ordered_tests if name == selected_test_name]

        source_verification_id = str(verification_details.get('artifact_verification_id') or verification_id_hint or '')
        if not is_canonical_artifact_id(source_verification_id):
            source_verification_id = ''

        def _verification_artifact_preview(verification_id: str, rel_path: str) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_rel_path = (rel_path or '').lstrip('/')
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_verification_id)):
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/artifacts/{safe_verification_id}/{safe_rel_path}'
            resolved = verification_artifact_blob(safe_verification_id, safe_rel_path)
            if resolved is None:
                return _run_detail_preview_unavailable('missing')
            blob, _filename = resolved
            return _run_detail_preview_from_bytes(blob, download_href)

        def _verification_output_preview(verification_id: str, task_id: str, test_name: str) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_task_id = str(task_id or '').strip()
            test_stem = Path(test_name).stem
            filename = f'{test_stem}.out' if test_stem else 'program.out'
            if not problem_slug or not username or (not safe_task_id) or (not filename) or (not is_canonical_artifact_id(safe_verification_id)):
                return _run_detail_preview_unavailable('missing')
            virtual_rel = f'output/{safe_task_id}/{filename}'
            return _verification_artifact_preview(safe_verification_id, virtual_rel)

        def _verification_blob_preview(verification_id: str, rel_path: str) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_rel_path = (rel_path or '').lstrip('/')
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_verification_id)):
                return _run_detail_preview_unavailable('missing')
            if not safe_rel_path.startswith('cache://'):
                return _run_detail_preview_unavailable('missing')
            virtual_rel = verification_blob_virtual_rel(safe_rel_path, filename=Path(safe_rel_path).name)
            if not virtual_rel:
                return _run_detail_preview_unavailable('missing')
            return _verification_artifact_preview(safe_verification_id, virtual_rel)

        for test_name in target_tests:
            row_index = int(row_index_by_test.get(test_name) or 0)
            if row_index <= 0:
                continue
            generate_task_row = generate_task_by_test_name.get(test_name)
            generate_detail = (
                _generate_detail_from_task_row(
                    generate_task_row,
                    tests_meta_row=tests_meta_by_test_name.get(test_name),
                )
                if generate_task_row is not None
                else None
            )
            input_rel = f'tests/{test_name}'
            answer_name = _run_test_answer_name(test_name)
            answer_rel = f'ans/{answer_name}' if answer_name else ''
            input_preview = _verification_artifact_preview(source_verification_id, input_rel)
            answer_preview = _verification_artifact_preview(source_verification_id, answer_rel) if answer_rel else _run_detail_preview_unavailable('missing')
            cells: list[dict] = []
            for col in columns:
                cell = col['tests_map'].get(test_name)
                if cell is None:
                    cells.append({'text': '--', 'short': '--', 'metrics': '-', 'kind': 'neutral', 'text_tone': '', 'detail': None})
                    continue
                detail_raw = cell.get('detail')
                detail_payload = dict(detail_raw) if detail_raw is not None else None
                if detail_payload is not None:
                    pass_rows_payload: list[dict[str, object]] = []
                    pass_rows_raw = detail_payload.get('pass_rows') or []
                    for pass_item in pass_rows_raw:
                        row_payload = dict(pass_item)
                        output_rel = (row_payload.get('output_rel') or '')
                        output_task_id = str(row_payload.get('output_task_id') or '')
                        output_preview = _run_detail_preview_unavailable('missing')
                        if output_rel:
                            if output_task_id and source_verification_id:
                                output_preview = _verification_output_preview(source_verification_id, output_task_id, test_name)
                            else:
                                output_preview = _verification_blob_preview(source_verification_id, output_rel)
                        row_payload['output_preview'] = output_preview
                        checker_log_rel = (row_payload.get('checker_log_rel') or '')
                        feedback_rel = (row_payload.get('feedback_rel') or '')
                        feedback_preview = _run_detail_preview_unavailable('missing')
                        if feedback_rel:
                            feedback_preview = _verification_blob_preview(source_verification_id, feedback_rel)
                        elif checker_log_rel:
                            feedback_preview = _verification_blob_preview(source_verification_id, checker_log_rel)
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
                    interactive_mode = (col.get('mode') or '') == 'interactive'
                    if interactive_mode and output_preview is not None:
                        final_row_payload['interactive_transcript'] = _interactive_transcript_preview(output_preview)
                    detail_payload['final_row'] = final_row_payload
                cells.append({'text': (cell['text']), 'short': (cell.get('short') or cell.get('text') or '--'), 'metrics': (cell.get('metrics') or '-'), 'kind': (cell['kind']), 'text_tone': (cell.get('text_tone') or ''), 'detail': detail_payload})
            if detail_is_main_correct_run:
                for cell in cells:
                    detail_payload = cell.get('detail') or {}
                    final_row_payload = detail_payload.get('final_row') or {}
                    output_preview = final_row_payload.get('output_preview')
                    if output_preview is not None and bool(output_preview.get('available')):
                        answer_preview = output_preview
                        break
            generate_note = dict(row_generate_notes.get(test_name) or {})
            test_cell = _test_name_cell(
                actual_test_name=test_name,
                fallback_name=test_name,
                is_placeholder=False,
                note=generate_note,
                has_detail=bool(generate_detail is not None or any((cell.get('detail') is not None for cell in cells))),
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
                    'generate_detail': generate_detail,
                    'cells': cells,
                    'has_detail': bool(generate_detail is not None or any((cell.get('detail') is not None for cell in cells))),
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
    if (not verification_details) and verification_id:
        detail_snapshot = load_verification_detail_summary(problem_id, verification_id)
        detail_details = detail_snapshot.get('details')
        if detail_details is not None:
            verification_details = dict(detail_details)
        if not verification_created_at:
            verification_created_at = (detail_snapshot.get('created_at') or '')
    if has_task_graph:
        task_counts = _task_counts_from_rows(task_rows)
        running_tasks = _running_tasks_from_rows(task_rows)
    else:
        task_counts = dict(verification_details.get('task_counts') or {}) if isinstance(verification_details.get('task_counts'), dict) else {}
        running_tasks = list(verification_details.get('running_tasks') or []) if isinstance(verification_details.get('running_tasks'), list) else []
    detail_fail_reason = str((verification_record.get('fail_reason') if verification_record is not None else '') or '')
    detail_fail_flag = bool(detail_fail_reason)
    detail_fail_reason = _rewrite_failure_reason_with_source(detail_fail_reason, columns)
    detail_fail_flag = bool(detail_fail_reason)
    detail_sanity = _detail_sanity_context(verification_id, verification_details)
    detail_status = str(status_summary['status'])
    detail_sanity_status = str(detail_sanity.get('status') or '')
    detail_status_display = detail_status
    if detail_status == "ok" and detail_sanity_status == "warning":
        detail_status_display = "ok (has warning)"
    elif detail_status == "ok" and detail_sanity_status == "failed":
        detail_status_display = "ok (sanity failed)"
    detail_status_tone = "warn" if detail_status == "ok" and detail_sanity_status in {"warning", "failed"} else detail_status
    stage_results = verification_details.get('stage_results') if isinstance(verification_details.get('stage_results'), dict) else {}
    verification_logs: dict[str, object] = {
        'available': False,
        'title': 'Verification',
        'verification_id': '',
        'status': '',
        'error': '',
        'error_display': '',
        'log_rows': [],
        'diagnostics': [],
    }

    if source_verification_id and problem_slug and username:
        artifact_verification_status = verification_record['status'] if verification_record is not None else ''
        artifact_verification_error = detail_fail_reason
        diagnostics_title = 'Verification'
        log_rows: list[dict[str, str]] = []
        diagnostics_rows: list[dict[str, object]] = []
        for col in columns:
            raw_diags = col.get('compile_diagnostics') or []
            if not raw_diags:
                continue
            normalized_diags = _normalize_diagnostics(raw_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
            diagnostics_title = str(col.get('title') or 'Verification')
            break
        if not diagnostics_rows:
            verification_diags = verification_details.get('compile_diagnostics') or []
            stage_generate = cast(dict[str, object], stage_results.get('generate_input') or {}) if 'generate_input' in stage_results else {}
            if not verification_diags:
                verification_diags = stage_generate.get('compile_diagnostics') or []
            if verification_diags:
                normalized_diags = _normalize_diagnostics(list(verification_diags), _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
                diagnostics_title = 'Verification'
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
        source_aware_column_reason = _rewrite_failure_reason_with_source("", columns)
        artifact_verification_error = _rewrite_failure_reason_with_source(artifact_verification_error, columns)
        generic_column_reasons = {
            str(col.get('match_reason') or '').strip()
            for col in columns
            if str(col.get('match_reason') or '').strip()
        }
        if (not diagnostics_rows) and (
            artifact_verification_error in generic_column_reasons
            or (source_aware_column_reason and artifact_verification_error == source_aware_column_reason)
        ):
            artifact_verification_error = ''
        verification_logs = {
            'available': True,
            'title': diagnostics_title,
            'verification_id': source_verification_id,
            'status': artifact_verification_status,
            'error': artifact_verification_error,
            'error_display': run_error_display(artifact_verification_error),
            'log_rows': log_rows,
            'diagnostics': diagnostics_rows,
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
        'detail_status': detail_status,
        'detail_status_display': detail_status_display,
        'detail_status_tone': detail_status_tone,
        'detail_is_main_correct_run': bool(detail_is_main_correct_run),
        'detail_running': bool(status_summary['has_running']),
        'detail_last_updated': last_updated,
        'detail_progress_total': progress_total,
        'detail_progress_reported': progress_reported,
        'detail_progress_placeholder_total': progress_placeholder_total,
        'detail_task_counts': task_counts,
        'detail_running_tasks': running_tasks,
        'detail_fail_flag': detail_fail_flag,
        'detail_fail_reason': detail_fail_reason,
        'detail_sanity': detail_sanity,
        'detail_verification_logs': verification_logs,
    }
