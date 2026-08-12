from __future__ import annotations

import app.main_constant as _K
import json
from pathlib import Path
from typing import cast
from app.impl.runtime.dependency import runtime
from app.impl.workspace.artifact import verification_artifact_file, verification_blob_virtual_rel
from app.impl.workspace.context import count_label
from app.impl.workspace.problem_config import read_problem_config
from app.impl.workspace.context_operation import (
    solution_metadata_entry,
    workspace_rel_file_exists,
)
from app.impl.workspace.context_run_detail import (
    _cap_run_test_feedback_files,
    _cap_summary_list,
    _decorate_compile_diagnostics,
    _normalize_diagnostics,
    _run_detail_preview_from_bytes,
    normalize_run_id_token,
    normalize_run_test_name_token,
    _run_detail_preview_unavailable,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
    _run_source_from_summary,
)
from app.impl.workspace.context_verification import normalize_program_id_token
from app.service.judgehost.case_result import decode_case_test_row
from app.service.judgehost.runpipe_transcript import parse_runpipe_transcript
from app.service.platform.error_text import bounded_display_text
from app.service.platform.workspace_path import (
    normalize_optional_component_source_path_safe,
    normalize_workspace_rel_path,
)
from app.service.verification.runtime import effective_run_timeout_ms
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    normalize_expected_behavior,
)
from app.service.verification.task_store import VerificationTaskRow
from app.service.verification.types import VerificationTaskStatus
from app.service.platform.process import is_canonical_artifact_id
from app.impl.workspace.run_view_lifecycle_card import (
    load_verification_detail_summary,
    _verification_tests_meta_stats,
)
from app.impl.workspace.run_test_generation import (
    build_test_generation_views,
    generation_warning_message,
)
from app.service.verification.result_match import (
    expected_status_rule,
    run_actual_short,
    run_verdict_short,
    status_rule_expected_display,
    verification_solution_match,
)
from app.service.verification.failure_display import verification_solution_failure_hint
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
    rewrite_failure_reason_with_source,
    run_actual_display,
    run_cpu_wall_ms_text,
    run_error_display,
    run_memory_mb_text,
)
from app.service.verification.runtime_threshold import (
    SUMMARY_RUNTIME_THRESHOLD_CHECK,
    evaluate_summary_runtime_threshold,
    time_limit_ms_from_run_config_json,
)

_TASK_KIND_GENERATE_INPUT = "generate-input"
_TASK_KIND_MAIN_CORRECT = "main-correct"
_TASK_KIND_SOLUTION_RUN = "solution-run"
_SANITY_STATUS_TOKENS = {"ok", "passed", "pending", "running", "warning", "failed", "skipped"}
_SANITY_CHECK_ORDER = (
    "empty_output_stability",
    "unicode_output_stability",
    "custom_sample_output",
    SUMMARY_RUNTIME_THRESHOLD_CHECK,
    "boundary_coverage",
)
_SANITY_CHECK_LABELS = {
    "empty_output_stability": "Empty output stability",
    "unicode_output_stability": "Unicode output stability",
    "custom_sample_output": "Custom sample output",
    SUMMARY_RUNTIME_THRESHOLD_CHECK: "Summary runtime threshold",
    "boundary_coverage": "Boundary coverage",
}


def _run_cell_text_tone(verdict: str, expected_behavior: str) -> str:
    _ = verdict, expected_behavior
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
        "sanity_check_results": [
            dict(item)
            for item in cast(list[object], verification_details.get("sanity_check_results") or [])
            if isinstance(item, dict)
        ],
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
    error = bounded_display_text(
        str(payload.get("error") or ""),
        limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
    )
    if error:
        return error
    status = str(payload.get("sanity_status") or "")
    failed_check = str(payload.get("failed_check") or "")
    if status == "warning" and failed_check:
        return f"{_sanity_check_label(failed_check)} has warning"
    if status == "failed" and failed_check:
        return f"{_sanity_check_label(failed_check)} failed"
    return ""


def _sanity_messages(raw_messages: object) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    for raw in cast(list[object], raw_messages or []):
        if not isinstance(raw, dict):
            continue
        message = bounded_display_text(
            str(raw.get("message") or ""),
            limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
        if not message:
            continue
        messages.append(
            {
                "severity": str(raw.get("severity") or ""),
                "test_name": str(raw.get("test_name") or ""),
                "message": message,
            }
        )
    return messages


def _sanity_task_rows_from_results(payload: dict[str, object]) -> list[dict[str, object]]:
    status = str(payload.get("sanity_status") or "")
    results = [
        dict(item)
        for item in cast(list[object], payload.get("sanity_check_results") or [])
        if isinstance(item, dict)
    ]
    if not results:
        failed_check = str(payload.get("failed_check") or "")
        if failed_check and status in {"warning", "failed"}:
            message = bounded_display_text(
                str(payload.get("error") or _sanity_reason(payload)),
                limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
            )
            messages = (
                [
                    {
                        "severity": status,
                        "test_name": str(payload.get("failed_test") or ""),
                        "message": message,
                    }
                ]
                if message
                else []
            )
            return [
                {
                    "name": failed_check,
                    "label": _sanity_check_label(failed_check),
                    "status": status,
                    "tone": _sanity_status_tone(status),
                    "detail": "" if messages else bounded_display_text(
                        _sanity_reason(payload),
                        limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
                    ),
                    "messages": messages,
                }
            ]
        return []
    rows: list[dict[str, object]] = []
    for item in results:
        check_name = str(item.get("name") or item.get("check_name") or "")
        if not check_name:
            continue
        row_status = str(item.get("status") or "")
        if not row_status:
            row_status = status if status in {"pending", "running", "skipped"} else "passed"
        messages = _sanity_messages(item.get("messages"))
        detail = ""
        if not messages:
            if row_status == "passed":
                detail = "completed"
            elif row_status == "pending":
                detail = "waiting for sanity checks"
            elif row_status == "running":
                detail = "running"
            elif row_status == "skipped":
                detail = "not run"
        rows.append(
            {
                "name": check_name,
                "label": _sanity_check_label(check_name),
                "status": row_status,
                "tone": _sanity_status_tone(row_status),
                "detail": bounded_display_text(
                    detail,
                    limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
                ),
                "messages": messages,
            }
        )
    return rows


def _sanity_task_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    return _sanity_task_rows_from_results(payload)


def _detail_sanity_context(
    verification_id: str,
    verification_details: dict[str, object],
) -> dict[str, object]:
    if not verification_id:
        return {
            "available": False,
            "status": "unknown",
            "reason": "",
            "tasks": [],
            "attention_tasks": [],
            "task_count": 0,
            "ran_count": 0,
            "checked_count": 0,
        }
    payload = _build_sanity_payload(verification_details)
    tasks = _sanity_task_rows(payload)
    attention_tasks = [
        task
        for task in tasks
        if str(task.get("status") or "") in {"warning", "failed"} or bool(task.get("messages"))
    ]
    return {
        "available": True,
        "status": str(payload["sanity_status"]),
        "reason": _sanity_reason(payload),
        "tasks": tasks,
        "attention_tasks": attention_tasks,
        "task_count": len(tasks),
        "ran_count": sum(
            1 for task in tasks if str(task.get("status") or "") in {"passed", "warning", "failed"}
        ),
        "checked_count": int(payload["sanity_checked_count"]),
    }


def build_run_detail_context(
    ctx: dict,
    execute_mode: str,
    *,
    requested_verification_id: str = '',
    include_row_details: bool = False,
    detail_test_name: str = '',
    detail_program_id: str = '',
) -> dict:
    display_limit = int(
        runtime().config_values.snapshot()["AUX_DISPLAY_TEXT_LIMIT_BYTES"]
    )

    def _task_graph_column_keys_from_task_rows(
        rows: list[VerificationTaskRow],
    ) -> list[str]:
        values: list[str] = []
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            program_id = str(row['program_id'] or '')
            if program_id and program_id not in values:
                values.append(program_id)
        return values

    def _program_ids_from_task_rows(rows: list[VerificationTaskRow]) -> list[str]:
        return _task_graph_column_keys_from_task_rows(rows)

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
            display_status = 'running' if status == VerificationTaskStatus.LEASED else status
            counts['total'] = int(counts['total']) + 1
            if display_status in counts:
                counts[display_status] = int(counts[display_status]) + 1
        return counts

    def _running_tasks_from_rows(rows: list[VerificationTaskRow]) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        for row in rows:
            if row['status'] != VerificationTaskStatus.LEASED:
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

    def _task_graph_task_status_by_program_and_test(
        rows: list[VerificationTaskRow],
    ) -> dict[tuple[str, str], str]:
        values: dict[tuple[str, str], str] = {}
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            program_id = str(row['program_id'] or '')
            test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
            if (not program_id) or (not test_name):
                continue
            values[(program_id, test_name)] = str(row['status'] or '')
        return values

    def _late_task_diagnostic_text(row: VerificationTaskRow) -> str:
        rendered = str(row.get("late_diagnostic_text") or "")
        if rendered:
            return bounded_display_text(rendered, limit_bytes=display_limit)
        raw_items = row.get("late_diagnostics")
        if not isinstance(raw_items, list):
            return ""
        messages: list[str] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            text = str(raw_item.get("text") or "")
            if not text:
                continue
            kind = str(raw_item.get("kind") or "diagnostic")
            hostname = str(raw_item.get("hostname") or "unknown host")
            received_at = str(raw_item.get("received_at") or "")
            label = f"late {kind} from {hostname}"
            if received_at:
                label = f"{label} at {received_at}"
            messages.append(f"[{label}]\n{text}")
        return bounded_display_text(
            "\n\n".join(messages),
            limit_bytes=display_limit,
        )

    def _task_graph_fallback_rows(
        rows: list[VerificationTaskRow],
        *,
        verification_record: dict[str, str] | None,
        verification_details: dict[str, object],
        verification_mode: str,
    ) -> dict[str, dict[str, object]]:
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            task_kind = str(row['task_kind'] or '')
            if task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}:
                continue
            source_path = str(row['source_path'] or '')
            if not source_path:
                continue
            program_id = str(row['program_id'] or '')
            if not program_id:
                continue
            item = grouped.setdefault(
                program_id,
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
        for program_id, item in grouped.items():
            statuses = list(cast(list[str], item['statuses']))
            grouped_rows = sorted(
                cast(list[VerificationTaskRow], item['rows']),
                key=lambda current: (_run_test_sort_key(str(current['test_name'] or '')), str(current['id'] or '')),
            )
            if VerificationTaskStatus.LEASED in statuses:
                status = 'running'
            elif VerificationTaskStatus.QUEUED in statuses:
                status = 'queued'
            elif VerificationTaskStatus.PENDING in statuses:
                status = 'pending'
            elif VerificationTaskStatus.FAILED in statuses:
                status = 'failed'
            elif VerificationTaskStatus.CANCELLED in statuses:
                status = 'cancelled'
            elif grouped_rows and all(
                current['status'] == VerificationTaskStatus.DONE
                for current in grouped_rows
            ):
                status = 'ok'
            else:
                status = 'pending'
            tests: list[dict[str, object]] = []
            compile_log = ''
            compile_diagnostics: list[dict[str, object]] = []
            error_text = ''
            late_diagnostic_texts: list[str] = []
            max_time_ms = 0
            max_memory_kb = 0
            for row in grouped_rows:
                row_status = str(row['status'] or '')
                if (
                    row_status in {VerificationTaskStatus.DONE, VerificationTaskStatus.FAILED}
                    and str(row['verdict'] or '').upper() != 'SK'
                ):
                    test_row = decode_case_test_row(
                        row['result'],
                        test_name=str(row['test_name'] or ''),
                    )
                    late_diagnostic_text = _late_task_diagnostic_text(row)
                    if late_diagnostic_text:
                        canonical_message = str(test_row.get('message') or '')
                        test_row['message'] = bounded_display_text(
                            "\n\n".join(
                                value
                                for value in (
                                    canonical_message,
                                    late_diagnostic_text,
                                )
                                if value
                            ),
                            limit_bytes=display_limit,
                        )
                    test_row['late_diagnostics'] = list(
                        cast(list[object], row.get('late_diagnostics') or [])
                    )
                    test_row['late_diagnostic_text'] = late_diagnostic_text
                    tests.append(test_row)
                    runtime_ms = int(test_row.get('time_ms') or 0)
                    cpu_ms = int(test_row.get('time_user_ms') or runtime_ms)
                    memory_kb = int(test_row.get('memory_kb') or 0)
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
                late_diagnostic_text = _late_task_diagnostic_text(row)
                if (
                    late_diagnostic_text
                    and late_diagnostic_text not in late_diagnostic_texts
                ):
                    late_diagnostic_texts.append(late_diagnostic_text)
            if late_diagnostic_texts:
                error_text = bounded_display_text(
                    "\n\n".join(
                        value
                        for value in (error_text, *late_diagnostic_texts)
                        if value
                    ),
                    limit_bytes=display_limit,
                )
            summary: dict[str, object] = {
                'mode': verification_mode,
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
            if VerificationTaskStatus.CANCELLED in statuses:
                summary['cancelled'] = True
                if verification_status in {'failed', 'cancelled'} and verification_error:
                    summary['error'] = summary['error'] or verification_error
            values[program_id] = {
                'id': program_id,
                'artifact_verification_id': verification_details.get('artifact_verification_id') or requested_verification_id or '',
                'mode': verification_mode,
                'status': status,
                'source_label': str(item['source_path']),
                'summary': summary,
                'created_at': verification_record['created_at'] if verification_record is not None else '',
                'finished_at': _latest_iso_timestamp([str(current['finished_at'] or '') for current in grouped_rows]) or (verification_record['finished_at'] if verification_record is not None else ''),
            }
        return values

    def _missing_solution_cell(task_status: str) -> dict[str, object]:
        if task_status == VerificationTaskStatus.LEASED:
            return {
                'text': '..',
                'short': '..',
                'metrics': 'running',
                'kind': 'running',
                'text_tone': '',
                'detail': None,
            }
        if task_status == VerificationTaskStatus.FAILED:
            return {
                'text': 'FL',
                'short': 'FL',
                'metrics': 'failed',
                'kind': 'fail',
                'text_tone': '',
                'detail': None,
            }
        if task_status == VerificationTaskStatus.CANCELLED:
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
    problem_id = int(ctx['problem']['id'])
    workspace_id = int(ctx['workspace']['id'])
    problem_slug = ctx['problem']['slug']
    username = ctx['user']['username']
    fallback_time_limit_ms = 0
    fallback_timeout_ms = 0
    try:
        _payload, general_cfg, _cfg_path = read_problem_config(workspace)
        fallback_time_limit_ms = general_cfg['time_limit_ms']
        fallback_timeout_ms = effective_run_timeout_ms(
            fallback_time_limit_ms,
            mode=general_cfg['mode'],
            pass_limit=general_cfg['pass_limit'],
            default_ms=int(_K.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            min_ms=int(runtime().config_values.GENERAL_TIME_LIMIT_MIN_MS),
            max_ms=int(runtime().config_values.GENERAL_TIME_LIMIT_MAX_MS),
            pass_fail_slack_sec=int(runtime().config_values.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC),
            multi_pass_slack_sec=int(runtime().config_values.RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC),
            interactive_slack_sec=int(runtime().config_values.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC),
        )
    except Exception:
        fallback_time_limit_ms = 0
        fallback_timeout_ms = 0
    selected_program_ids: list[str] = []
    verification_program_rows: dict[str, dict[str, object]] = {}
    verification_id_hint = normalize_run_id_token(requested_verification_id)
    verification_details: dict[str, object] = {}
    task_rows: list[VerificationTaskRow] = []
    has_task_graph = False
    source_verification_id = verification_id_hint if is_canonical_artifact_id(verification_id_hint) else ''
    verification_snapshot = (
        runtime().verification_service.verification_snapshot(verification_id_hint)
        if verification_id_hint
        else None
    )
    verification_record = (
        verification_snapshot["record"]
        if verification_snapshot is not None
        else None
    )
    verification_access = runtime().access_query.verification_context(
        actor_user_id=int(ctx["user"]["id"]),
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verification=verification_record,
    )
    verification_visible = verification_access["can_view"]
    if verification_visible and verification_record is not None:
        task_rows = cast(
            list[VerificationTaskRow],
            verification_snapshot["tasks"],
        )
        has_task_graph = bool(task_rows)
        verification_detail_payload = verification_snapshot["detail"]
        verification_details = {
            **verification_detail_payload,
            **runtime().verification_service.verification_runtime_summary_from_tasks(
                cast(list[dict[str, object]], verification_snapshot["tasks"])
            ),
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
        verification_mode = str(verification_details.get('mode') or '')
        if verification_mode not in {'pass-fail', 'interactive'}:
            verification_mode = 'malformed'
        if has_task_graph:
            verification_program_rows = _task_graph_fallback_rows(
                task_rows,
                verification_record=verification_record,
                verification_details=verification_details,
                verification_mode=verification_mode,
            )
            if not selected_program_ids:
                selected_program_ids.extend(
                    _task_graph_column_keys_from_task_rows(
                        task_rows,
                    )
                )
        else:
            raw_programs = verification_details.get('programs') if isinstance(verification_details.get('programs'), dict) else {}
            raw_program_order = verification_details.get('program_order') if isinstance(verification_details.get('program_order'), list) else []
            selected_program_ids = [str(item or '').strip() for item in raw_program_order if str(item or '').strip()]
            if not selected_program_ids:
                selected_program_ids = [str(key or '').strip() for key in raw_programs.keys() if str(key or '').strip()]
            for program_id in selected_program_ids:
                program_payload = raw_programs.get(program_id)
                if not isinstance(program_payload, dict):
                    continue
                program_summary = dict(program_payload.get('summary') or {})
                verification_program_rows[program_id] = {
                    'id': program_id,
                    'status': str(program_payload.get('status') or verification_details.get('status') or 'running'),
                    'mode': verification_mode,
                    'created_at': str(verification_details.get('created_at') or verification_record['created_at'] if verification_record is not None else ''),
                    'finished_at': str(verification_details.get('finished_at') or verification_record['finished_at'] if verification_record is not None else ''),
                    'artifact_verification_id': str(verification_details.get('artifact_verification_id') or verification_id_hint or ''),
                    'summary': program_summary,
                    'source_label': str(program_payload.get('source_label') or program_summary.get('source') or program_id),
                    'task_kind': str(program_payload.get('task_kind') or program_summary.get('task_kind') or ''),
                }
    runtime_threshold_time_limit_ms = time_limit_ms_from_run_config_json(
        str(verification_details.get('run_config_json') or ''),
        default_ms=fallback_time_limit_ms,
    )
    verification_created_at = ''
    if not verification_created_at and verification_record is not None:
        verification_created_at = verification_record['created_at']
    solutions = []
    preferred_solution_program_ids = _program_ids_from_task_rows(task_rows) if has_task_graph else []
    if preferred_solution_program_ids:
        selected_program_ids = list(preferred_solution_program_ids)
    expected_by_program_id: dict[str, str] = {}
    expected_by_source: dict[str, str] = {}
    for item in solutions:
        expected_token = normalize_expected_behavior((item.get('expected_behavior') or 'unknown'))
        if expected_token == 'unknown':
            continue
        program_token = normalize_program_id_token(item.get('program_id'))
        if program_token and program_token not in expected_by_program_id:
            expected_by_program_id[program_token] = expected_token
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
            expected_token = "unknown"
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
        elif tone == 'warn':
            kind = 'warn'
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
    tests_meta_by_test_name: dict[str, dict[str, object]] = {}
    for item in cast(list[object], verification_details.get('tests_meta_rows') or []):
        if not isinstance(item, dict):
            continue
        test_name = normalize_run_test_name_token(str(item.get('test_name') or ''))
        if test_name and test_name not in tests_meta_by_test_name:
            tests_meta_by_test_name[test_name] = dict(item)
    test_generation_views = build_test_generation_views(
        task_rows,
        tests_meta_by_test_name,
        limit_bytes=display_limit,
    )
    row_generate_notes: dict[str, dict[str, str]] = {
        test_name: {
            'tone': str(view['tone']),
            'status_label': str(view['status_label']),
            'text': str(view['table_text']),
            'detail': str(view['alert_message'] or view['detail']),
        }
        for test_name, view in test_generation_views.items()
    }
    task_graph_task_status_by_program_and_test: dict[tuple[str, str], str] = {}
    if has_task_graph:
        task_graph_task_status_by_program_and_test = _task_graph_task_status_by_program_and_test(
            task_rows,
        )
        for row in task_rows:
            test_name = normalize_run_test_name_token(str(row['test_name'] or ''))
            if test_name:
                all_tests.add(test_name)
    selected_test_name_hint = normalize_run_test_name_token(detail_test_name) if include_row_details else ''
    domjudge_case_cells_by_program: dict[str, dict[str, dict[str, object]]] = {}
    for program_id in selected_program_ids:
        row = verification_program_rows.get(program_id)
        status = 'running'
        mode = (
            str(verification_details.get('mode') or 'malformed')
            if verification_record is not None
            else execute_mode
        )
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
        if mode not in {'pass-fail', 'interactive'}:
            mode = 'malformed'
        _cap_summary_list(summary, 'tests', runtime().config_values.RUN_DETAIL_TEST_LIST_LIMIT, 'tests_truncated', 'tests_total', 'tests_limit')
        raw_compile_diags = list(cast(list[object], summary.get('compile_diagnostics') or []))
        if raw_compile_diags:
            summary['compile_diagnostics'] = raw_compile_diags[: runtime().config_values.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
        if include_row_details:
            _cap_run_test_feedback_files(summary, runtime().config_values.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
        compile_diags = summary.get('compile_diagnostics') or []
        if compile_diags:
            normalized_diags = _normalize_diagnostics(compile_diags, runtime().config_values.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        detail_compile_diagnostics = list(cast(list[dict[str, object]], summary.get('compile_diagnostics') or []))
        detail_compile_error = bounded_display_text(
            summary.get('error') or '',
            limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
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
            title = program_id or 'unknown program'
        source_section = ''
        source_path = ''
        source_rel = normalize_workspace_rel_path(source_for_display)
        if problem_slug and username and source_rel and workspace_rel_file_exists(workspace, source_rel):
            safe_solution = normalize_optional_component_source_path_safe(source_rel, 'solutions', 'solution path')
            if safe_solution:
                source_section = 'solutions'
                source_path = safe_solution
            else:
                source_section = 'files'
                source_path = source_rel
        expected_behavior = _run_expected_behavior_from_summary(summary)
        if expected_behavior == 'unknown':
            mapped_expected = expected_by_program_id.get(program_id)
            if not mapped_expected and source_rel:
                mapped_expected = expected_by_source.get(source_rel)
            if not mapped_expected and source_rel:
                mapped_expected = _expected_from_workspace_source(source_rel)
            if mapped_expected:
                expected_behavior = mapped_expected
        matched, completed, observed_pass, match_reason = verification_solution_match(expected_behavior, status, summary)
        required_codes, allowed_codes = expected_status_rule(expected_behavior)
        expected_display = status_rule_expected_display(expected_behavior)
        expected_is_ac_only = bool(required_codes == ('AC',) and allowed_codes == ('AC',))
        got_short = run_actual_short(status, summary)
        got_display = run_actual_display(status, summary)
        result_kind = _run_cell_kind(got_short, expected_behavior) if got_short else 'neutral'
        result_text_tone = _run_cell_text_tone(got_short, expected_behavior)
        result_tone_class = f'tone-{result_kind}'
        expected_mismatch = bool(expected_behavior != 'unknown' and completed and (not matched))
        execution_skipped_from_summary = bool(summary.get('execution_skipped'))
        if not execution_skipped_from_summary and (summary.get('failure_stage') or '') == 'build':
            execution_skipped_from_summary = True
        tests_map: dict[str, dict] = {}
        max_time_ms = 0
        max_time_tone = ''
        max_memory_kb = 0
        has_test_metrics = False
        tests_raw = summary.get('tests') or []
        runtime_threshold_report = evaluate_summary_runtime_threshold(
            summary=summary,
            source=source_for_display,
            time_limit_ms=runtime_threshold_time_limit_ms,
        )
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
                time_tone = 'warn' if str(test_name) in runtime_threshold_report.highlighted_tests else ''
                has_test_metrics = True
                if time_ms > max_time_ms:
                    max_time_ms = time_ms
                    max_time_tone = time_tone
                if memory_kb > max_memory_kb:
                    max_memory_kb = memory_kb
                detail_payload: dict[str, object] | None = None
                if include_row_details:
                    passes_raw = item.get('passes')
                    late_diagnostic_text = bounded_display_text(
                        item.get('late_diagnostic_text') or '',
                        limit_bytes=display_limit,
                    )
                    feedback_display = '-'
                    inline_feedback = bounded_display_text(
                        item.get('message') or item.get('error') or '',
                        limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
                    )
                    feedback_files = item.get('feedback_files') or []
                    feedback_items: list[str] = []
                    for feedback_entry in feedback_files:
                        token = (feedback_entry or '')
                        if token:
                            feedback_items.append(token)
                    test_stem = Path(str(test_name)).stem
                    checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                    feedback_rel = feedback_items[0] if feedback_items else ''
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
                    pass_rows: list[dict[str, object]] = []
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
                            pass_feedback = bounded_display_text(
                                pass_item.get('feedback') or '',
                                limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
                            )
                            row_feedback_display = pass_feedback or feedback_display
                            output_rel = (pass_item.get('output_ref') or '')
                            pass_number = int(pass_item['pass'])
                            pass_time_display = run_cpu_wall_ms_text(pass_time_user_ms, pass_time_wall_ms)
                            pass_memory_display = run_memory_mb_text(pass_memory_kb)
                            pass_rows.append(
                                {
                                    'pass_number': pass_number,
                                    'pass_label': f'Pass {pass_number}',
                                    'capture_status': str(pass_item.get('capture_status') or ''),
                                    'verdict_short': pass_verdict_short,
                                    'text_tone': _run_cell_text_tone(pass_verdict, expected_behavior),
                                    'kind': _run_cell_kind(pass_verdict, expected_behavior),
                                    'time_display': pass_time_display,
                                    'time_tone': time_tone,
                                    'memory_display': pass_memory_display,
                                    'status_display': f'{pass_verdict_short} \u00b7 {pass_time_display} \u00b7 {pass_memory_display}',
                                    'feedback_display': row_feedback_display,
                                    'output_task_id': str(item.get('task_id') or ''),
                                    'input_ref': str(pass_item.get('input_ref') or ''),
                                    'output_rel': str(output_rel),
                                    'transcript_rel': str(pass_item.get('transcript_ref') or ''),
                                    'judge_message_rel': str(pass_item.get('judge_message_ref') or ''),
                                    'checker_log_rel': checker_log_rel,
                                    'feedback_rel': feedback_rel,
                                }
                            )
                    if not pass_rows:
                        output_rel = (item.get('output_ref') or '')
                        output_task_id = str(item.get('task_id') or '')
                        time_display = run_cpu_wall_ms_text(time_user_ms, time_wall_ms)
                        pass_rows.append(
                            {
                                'pass_number': 1,
                                'pass_label': 'Pass 1',
                                'capture_status': '',
                                'verdict_short': verdict_short,
                                'text_tone': _run_cell_text_tone(verdict, expected_behavior),
                                'kind': _run_cell_kind(verdict, expected_behavior),
                                'time_display': time_display,
                                'time_tone': time_tone,
                                'memory_display': memory_mb_text,
                                'status_display': f'{verdict_short} \u00b7 {time_display} \u00b7 {memory_mb_text}',
                                'feedback_display': feedback_display,
                                'input_ref': '',
                                'output_rel': str(output_rel),
                                'output_task_id': output_task_id,
                                'transcript_rel': '',
                                'judge_message_rel': '',
                                'checker_log_rel': checker_log_rel,
                                'feedback_rel': feedback_rel,
                            }
                        )
                    final_index = len(pass_rows) - 1
                    for candidate_index in range(len(pass_rows) - 1, -1, -1):
                        candidate = pass_rows[candidate_index]
                        verdict_token = (candidate.get('verdict_short') or '')
                        if verdict_token and verdict_token not in {'--', '-'}:
                            final_index = candidate_index
                            break
                    if late_diagnostic_text and pass_rows:
                        final_feedback = str(
                            pass_rows[final_index].get('feedback_display') or ''
                        )
                        if late_diagnostic_text not in final_feedback:
                            pass_rows[final_index]['feedback_display'] = (
                                bounded_display_text(
                                    "\n\n".join(
                                        value
                                        for value in (
                                            "" if final_feedback == '-' else final_feedback,
                                            late_diagnostic_text,
                                        )
                                        if value
                                    ),
                                    limit_bytes=display_limit,
                                )
                            )
                    final_row = (
                        dict(pass_rows[final_index]) if pass_rows else {}
                    )
                    detail_payload = {
                        'verdict': verdict,
                        'verdict_short': verdict_short,
                        'time_display': f'{time_ms}ms',
                        'time_tone': time_tone,
                        'memory_display': memory_mb_text,
                        'status_display': f'{verdict_short} \u00b7 {run_cpu_wall_ms_text(time_user_ms, time_wall_ms)} \u00b7 {memory_mb_text}',
                        'feedback_display': feedback_display,
                        'pass_rows': pass_rows,
                        'final_row': final_row,
                        'is_multi_pass': bool(
                            int(verification_details.get('pass_limit') or 1) > 1
                            or len(pass_rows) > 1
                        ),
                        'compile_error_display': detail_compile_error,
                        'compile_diagnostics': detail_compile_diagnostics,
                        'late_diagnostics': list(
                            cast(list[object], item.get('late_diagnostics') or [])
                        ),
                    }
                all_tests.add(test_name)
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': verdict_short,
                    'short': verdict_short,
                    'metrics': f'{time_ms}ms/{memory_mb_text}',
                    'time_display': f'{time_ms}ms',
                    'time_tone': time_tone,
                    'memory_display': memory_mb_text,
                    'kind': _run_cell_kind(verdict, expected_behavior),
                    'text_tone': _run_cell_text_tone(verdict, expected_behavior),
                    'detail': detail_payload,
                    'detail_available': True,
                }
        execution_skipped = bool(execution_skipped_from_summary and (not has_materialized_tests))
        execution_skipped_reason = bounded_display_text(
            (summary.get('execution_skipped_reason') or summary.get('error') or ''),
            limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
        if (not has_task_graph) and (not execution_skipped):
            case_cells = domjudge_case_cells_by_program.get(program_id) or {}
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
                        'status_display': f'{short if short else "--"} \u00b7 {run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms)} \u00b7 {run_memory_mb_text(memory_kb)}',
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
                        'status_display': f'{short if short else "--"} \u00b7 {run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms)} \u00b7 {run_memory_mb_text(memory_kb)}',
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
        failure_display = (
            verification_solution_failure_hint(
                source_for_display,
                match_reason,
                str(summary.get('error') or ''),
                limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
            )
            if (match_reason or summary.get('error'))
            else ''
        )
        column_payload = {'id': program_id, 'artifact_verification_id': artifact_verification_id, 'title': title, 'source': source_for_display or '-', 'source_section': source_section, 'source_path': source_path, 'task_kind': task_kind, 'is_main_correct_run': bool(is_main_correct_run), 'status': status, 'mode': mode, 'created_at': created_at, 'finished_at': finished_at, 'summary': summary, 'has_run_row': bool(row is not None), 'tests_map': tests_map, 'compile_log': summary.get('compile_log') or '', 'compile_diagnostics': summary.get('compile_diagnostics') or [], 'error': summary.get('error') or '', 'error_display': run_error_display(summary.get('error') or ''), 'tests_total': int(summary.get('tests_total') or len(tests_map)), 'tests_truncated': bool(summary.get('tests_truncated')), 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_display': expected_display, 'expected_is_ac_only': bool(expected_is_ac_only), 'got_short': got_short, 'got_display': got_display, 'result_kind': result_kind, 'result_text_tone': result_text_tone, 'result_tone_class': result_tone_class, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': (match_reason or ''), 'execution_skipped': bool(execution_skipped), 'execution_skipped_reason': execution_skipped_reason, 'max_time_ms': int(max_time_ms), 'max_time_display': max_time_display, 'max_time_tone': max_time_tone, 'max_memory_kb': int(max_memory_kb), 'max_memory_display': max_memory_display}
        column_payload['failure_display'] = failure_display
        if (
            not _is_solution_column_source(source_for_display)
            and task_kind not in {_TASK_KIND_SOLUTION_RUN, _TASK_KIND_MAIN_CORRECT}
        ):
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
    selected_detail_program_id = (
        normalize_program_id_token(detail_program_id)
        if include_row_details
        else ''
    )
    if include_row_details and selected_detail_program_id:
        columns = [
            column
            for column in columns
            if str(column.get('id') or '') == selected_detail_program_id
        ]
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
    generation_diagnostic_message = generation_warning_message(test_generation_views, ordered_tests)
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
            generation_view = test_generation_views.get(actual_test_name) if actual_test_name else None
            generation_terminal = bool(generation_view is not None and generation_view['terminal'])
            generation_skipped = bool(generation_view is not None and generation_view['skipped'])
            if not generation_skipped:
                for col in columns:
                    cell = col['tests_map'].get(actual_test_name) if actual_test_name else None
                    if cell is None:
                        if has_task_graph and actual_test_name:
                            task_status = task_graph_task_status_by_program_and_test.get((str(col.get('id') or ''), actual_test_name), '')
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
                            'time_display': (cell.get('time_display') or ''),
                            'time_tone': (cell.get('time_tone') or ''),
                            'memory_display': (cell.get('memory_display') or ''),
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
                has_detail=bool(has_detail or generation_terminal),
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
                    'has_detail': bool((has_detail or generation_terminal) and (not is_placeholder)),
                    'test_source_kind': '' if generation_view is None else generation_view['source_kind'],
                    'test_command': '' if generation_view is None else generation_view['command'],
                    'generation_skipped': generation_skipped,
                    'generation_message': '' if generation_view is None else generation_view['alert_message'],
                }
            )
    else:
        selected_test_name = selected_test_name_hint
        target_tests = ordered_tests
        if selected_test_name:
            target_tests = [name for name in ordered_tests if name == selected_test_name]
        if selected_detail_program_id and not columns:
            target_tests = []

        source_verification_id = str(verification_details.get('artifact_verification_id') or verification_id_hint or '')
        if not is_canonical_artifact_id(source_verification_id):
            source_verification_id = ''

        def _verification_artifact_preview(verification_id: str, rel_path: str) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_rel_path = (rel_path or '').lstrip('/')
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_verification_id)):
                return _run_detail_preview_unavailable('missing')
            resolved = verification_artifact_file(safe_verification_id, safe_rel_path)
            if resolved is None:
                return _run_detail_preview_unavailable('missing')
            payload_file, _filename = resolved
            with payload_file.path.open('rb') as stream:
                blob = stream.read(int(runtime().config_values.RUN_DETAIL_PREVIEW_MAX_BYTES) + 1)
            return _run_detail_preview_from_bytes(
                blob,
                verification_id=safe_verification_id,
                rel_path=safe_rel_path,
            )

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
            if not safe_rel_path.startswith('blob://'):
                return _run_detail_preview_unavailable('missing')
            virtual_rel = verification_blob_virtual_rel(safe_rel_path, filename=Path(safe_rel_path).name)
            if not virtual_rel:
                return _run_detail_preview_unavailable('missing')
            return _verification_artifact_preview(safe_verification_id, virtual_rel)

        def _verification_transcript(
            verification_id: str,
            rel_path: str,
            *,
            unavailable_message: str,
        ) -> dict[str, object]:
            safe_verification_id = (verification_id or '')
            safe_rel_path = (rel_path or '').lstrip('/')
            unavailable = {
                'available': False,
                'state': 'unavailable',
                'events': [],
                'events_shown': 0,
                'events_total': 0,
                'events_omitted': 0,
                'raw_size_bytes': 0,
                'error_offset': None,
                'error_reason': None,
                'download_verification_id': '',
                'download_rel_path': '',
                'message': unavailable_message,
            }
            if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_verification_id)):
                return unavailable
            if not safe_rel_path.startswith('blob://'):
                return unavailable
            virtual_rel = verification_blob_virtual_rel(safe_rel_path, filename=Path(safe_rel_path).name)
            if not virtual_rel:
                return unavailable
            resolved = verification_artifact_file(safe_verification_id, virtual_rel)
            if resolved is None:
                return unavailable
            payload_file, _filename = resolved
            with payload_file.path.open('rb') as stream:
                parsed = parse_runpipe_transcript(
                    stream,
                    raw_size_bytes=payload_file.size,
                )
            return {
                'available': True,
                **parsed,
                'download_verification_id': safe_verification_id,
                'download_rel_path': virtual_rel,
                'message': '',
            }

        for test_name in target_tests:
            row_index = int(row_index_by_test.get(test_name) or 0)
            if row_index <= 0:
                continue
            generation_view = test_generation_views.get(test_name)
            generation_terminal = bool(generation_view is not None and generation_view['terminal'])
            generation_alert = (
                generation_view
                if generation_view is not None and generation_view['alert_message']
                else None
            )
            input_rel = f'tests/{test_name}'
            answer_name = _run_test_answer_name(test_name)
            answer_rel = f'ans/{answer_name}' if answer_name else ''
            row_is_interactive = any(
                (col.get('mode') or '') == 'interactive'
                and col['tests_map'].get(test_name) is not None
                for col in columns
            )
            input_preview = _run_detail_preview_unavailable('not applicable')
            answer_preview = _run_detail_preview_unavailable('not applicable')
            if not row_is_interactive:
                input_preview = _verification_artifact_preview(source_verification_id, input_rel)
                answer_preview = (
                    _verification_artifact_preview(source_verification_id, answer_rel)
                    if answer_rel
                    else _run_detail_preview_unavailable('missing')
                )
            cells: list[dict] = []
            for col in columns:
                cell = col['tests_map'].get(test_name)
                if cell is None:
                    cells.append({'text': '--', 'short': '--', 'metrics': '-', 'kind': 'neutral', 'text_tone': '', 'detail': None})
                    continue
                detail_raw = cell.get('detail')
                detail_payload = dict(detail_raw) if detail_raw is not None else None
                if detail_payload is not None:
                    interactive_mode = (col.get('mode') or '') == 'interactive'
                    pass_rows_payload: list[dict[str, object]] = []
                    pass_rows_raw = detail_payload.get('pass_rows') or []
                    for pass_item in pass_rows_raw:
                        row_payload = dict(pass_item)
                        output_rel = (row_payload.get('output_rel') or '')
                        output_task_id = str(row_payload.get('output_task_id') or '')
                        output_preview = _run_detail_preview_unavailable('missing')
                        if output_rel and not interactive_mode:
                            if output_task_id and source_verification_id:
                                output_preview = _verification_output_preview(source_verification_id, output_task_id, test_name)
                            else:
                                output_preview = _verification_blob_preview(source_verification_id, output_rel)
                        row_payload['output_preview'] = output_preview
                        capture_status = str(row_payload.get('capture_status') or '')
                        capture_complete = capture_status == 'complete'
                        input_ref = str(row_payload.get('input_ref') or '')
                        pass_input_preview = _run_detail_preview_unavailable(
                            'missing' if capture_complete else 'not captured'
                        )
                        if input_ref:
                            pass_input_preview = _verification_blob_preview(
                                source_verification_id,
                                input_ref,
                            )
                        row_payload['input_preview'] = pass_input_preview
                        if interactive_mode:
                            transcript_rel = str(row_payload.get('transcript_rel') or '')
                            row_payload['interactive_transcript'] = _verification_transcript(
                                source_verification_id,
                                transcript_rel,
                                unavailable_message='missing' if capture_complete else 'not captured',
                            )
                            judge_message_rel = str(row_payload.get('judge_message_rel') or '')
                            feedback_preview = _run_detail_preview_unavailable(
                                'missing' if capture_complete else 'not captured'
                            )
                            if judge_message_rel:
                                feedback_preview = _verification_blob_preview(
                                    source_verification_id,
                                    judge_message_rel,
                                )
                                feedback_preview['download_verification_id'] = ''
                                feedback_preview['download_rel_path'] = ''
                            row_payload['feedback_preview'] = feedback_preview
                        else:
                            checker_log_rel = str(row_payload.get('checker_log_rel') or '')
                            feedback_rel = str(row_payload.get('feedback_rel') or '')
                            feedback_preview = _run_detail_preview_unavailable('missing')
                            if feedback_rel:
                                feedback_preview = _verification_blob_preview(
                                    source_verification_id,
                                    feedback_rel,
                                )
                            elif checker_log_rel:
                                feedback_preview = _verification_blob_preview(
                                    source_verification_id,
                                    checker_log_rel,
                                )
                            row_payload['feedback_preview'] = feedback_preview
                            if (row_payload.get('feedback_display') or '-') == '-' and bool(feedback_preview.get('available')):
                                preview_text = str(feedback_preview.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                                first_line = next((line for line in preview_text.splitlines() if line), '')
                                if first_line:
                                    row_payload['feedback_display'] = (
                                        first_line[:157].rstrip() + '...'
                                        if len(first_line) > 160
                                        else first_line
                                    )
                        pass_rows_payload.append(row_payload)
                    detail_payload['pass_rows'] = pass_rows_payload
                    detail_payload['is_interactive'] = interactive_mode
                    detail_payload['mode_malformed'] = (col.get('mode') or '') == 'malformed'
                    final_row_raw = detail_payload.get('final_row')
                    final_row_payload = dict(final_row_raw) if final_row_raw is not None else {}
                    if pass_rows_payload:
                        final_row_payload = dict(pass_rows_payload[-1])
                        for candidate in reversed(pass_rows_payload):
                            verdict_token = (candidate.get('verdict_short') or '')
                            if verdict_token and verdict_token not in {'--', '-'}:
                                final_row_payload = dict(candidate)
                                break
                    detail_payload['final_row'] = final_row_payload
                cells.append(
                    {
                        'text': (cell['text']),
                        'short': (cell.get('short') or cell.get('text') or '--'),
                        'metrics': (cell.get('metrics') or '-'),
                        'time_display': (cell.get('time_display') or ''),
                        'time_tone': (cell.get('time_tone') or ''),
                        'memory_display': (cell.get('memory_display') or ''),
                        'kind': (cell['kind']),
                        'text_tone': (cell.get('text_tone') or ''),
                        'detail': detail_payload,
                    }
                )
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
                has_detail=bool(generation_terminal or any((cell.get('detail') is not None for cell in cells))),
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
                    'is_interactive': row_is_interactive,
                    'generate_detail': (
                        generation_view
                        if generation_view is not None
                        and generation_view['status'] == VerificationTaskStatus.FAILED
                        else None
                    ),
                    'generation_alert': generation_alert,
                    'generation_skipped': bool(generation_view is not None and generation_view['skipped']),
                    'test_source_kind': '' if generation_view is None else generation_view['source_kind'],
                    'test_command': '' if generation_view is None else generation_view['command'],
                    'cells': cells,
                    'has_detail': bool(generation_terminal or any((cell.get('detail') is not None for cell in cells))),
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
    verification_id = verification_id_hint if verification_visible else ""
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
    detail_fail_reason = rewrite_failure_reason_with_source(
        detail_fail_reason,
        columns,
        limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
    )
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
            normalized_diags = _normalize_diagnostics(raw_diags, runtime().config_values.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
            diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
            diagnostics_title = str(col.get('title') or 'Verification')
            break
        if not diagnostics_rows:
            verification_diags = verification_details.get('compile_diagnostics') or []
            stage_generate = cast(dict[str, object], stage_results.get('generate_input') or {}) if 'generate_input' in stage_results else {}
            if not verification_diags:
                verification_diags = stage_generate.get('compile_diagnostics') or []
            if verification_diags:
                normalized_diags = _normalize_diagnostics(list(verification_diags), runtime().config_values.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
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
        source_aware_column_reason = rewrite_failure_reason_with_source(
            "",
            columns,
            limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
        artifact_verification_error = rewrite_failure_reason_with_source(
            artifact_verification_error,
            columns,
            limit_bytes=int(runtime().config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES),
        )
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
        'owns_verification': verification_access["owns_verification"],
        'can_rejudge': verification_access["can_rejudge"],
        'rejudge_block_reason': verification_access["rejudge_block_reason"],
        'can_cancel': verification_access["can_cancel"],
        'cancel_disabled_reason': (
            verification_access["cancel_block_reason"]
            if verification_visible and not verification_access["can_cancel"] and bool(status_summary['has_running'])
            else ''
        ),
        'verification_scope_notice': (
            'Published verification \u00b7 the original record is read-only'
            if verification_visible
            and verification_record is not None
            and verification_record["workspace_id"] is None
            else (
                'Verification from another workspace \u00b7 the original record is read-only'
                if verification_visible and not verification_access["owns_verification"]
                else ''
            )
        ),
        'detail_columns': columns,
        'detail_rows': detail_rows,
        'selected_program_ids': selected_program_ids,
        'rerun_solution_paths': rerun_paths,
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
        'detail_generation_diagnostic': {
            'title': 'Test generation',
            'message': generation_diagnostic_message,
        } if generation_diagnostic_message else None,
        'detail_verification_logs': verification_logs,
    }
