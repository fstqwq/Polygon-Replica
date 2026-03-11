from __future__ import annotations
import json
import re
from pathlib import Path
from app.impl.auth.public import parse_iso_utc
from app.impl.runtime.config import config
from .problem_config import coerce_int, normalize_problem_mode
from .run_display import (
    run_actual_display,
    run_actual_short,
    run_cpu_wall_ms_text,
    run_error_display,
    run_memory_mb_text,
    run_verdict_short,
)
from .context_operation import (
    dedupe_preserve_order,
    _expected_status_rule,
    parse_summary_json,
    _verification_solution_match,
)
from .context_run_detail import (
    normalize_run_id_token,
    _run_invocation_status_summary,
    _run_rejudge_context_for_entries,
    _run_source_from_summary,
)
from app.main_util import (
    normalize_optional_component_source_path_safe,
)
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from .run_view_lifecycle_card import _run_test_count_from_summary

_C = config.constants

def _run_invocation_block(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return {}
    payload = summary.get('invocation')
    if isinstance(payload, dict):
        return payload
    return {}

def _run_invocation_id_from_summary(summary: dict | None, fallback_run_id: str) -> str:
    block = _run_invocation_block(summary)
    invocation_id = normalize_run_id_token(block.get('id')) if isinstance(block, dict) else ''
    return invocation_id or str(fallback_run_id or '').strip()

def _run_invocation_run_ids_from_summary(summary: dict | None) -> list[str]:
    block = _run_invocation_block(summary)
    run_ids_raw = block.get('run_ids') if isinstance(block, dict) else None
    if not isinstance(run_ids_raw, list):
        return []
    values: list[str] = []
    for raw in run_ids_raw:
        token = normalize_run_id_token(raw)
        if token:
            values.append(token)
    return dedupe_preserve_order(values)


def _run_invocation_source_from_summary(summary: dict | None) -> str:
    block = _run_invocation_block(summary)
    return str(block.get('source') or '').strip().lower() if isinstance(block, dict) else ''


def _run_is_main_correct_invocation_source(source: object) -> bool:
    return str(source or '').strip().lower() == 'build.solve'

def _run_invocation_maps_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> dict[str, list[str]]:
    cap = max(40, int(limit))
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT ?\n        ", [int(problem_id), int(actor_user_id), cap])
    invocation_to_runs: dict[str, list[str]] = {}
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        invocation_token = normalize_run_id_token(details.get('invocation_id'))
        if not invocation_token:
            continue
        candidates: list[str] = []
        primary = normalize_run_id_token(details.get('run_id'))
        if primary:
            candidates.append(primary)
        raw_run_ids = details.get('run_ids')
        if isinstance(raw_run_ids, list):
            for item in raw_run_ids:
                token = normalize_run_id_token(item)
                if token:
                    candidates.append(token)
        deduped = dedupe_preserve_order(candidates)
        if deduped:
            existing = invocation_to_runs.get(invocation_token)
            if isinstance(existing, list) and existing:
                invocation_to_runs[invocation_token] = dedupe_preserve_order([*existing, *deduped])
            else:
                invocation_to_runs[invocation_token] = deduped
    return invocation_to_runs


def _run_pending_invocations_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> list[dict[str, object]]:
    cap = max(40, int(limit))
    rows = config.db.fetch_all(
        """
        SELECT action,details_json,created_at
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start', 'run.cancel')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), cap],
    )
    pending: list[dict[str, object]] = []
    seen_invocations: set[str] = set()
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row["details_json"] or "{}"))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        invocation_token = normalize_run_id_token(details.get("invocation_id"))
        if not invocation_token or invocation_token in seen_invocations:
            continue
        action_token = str(row["action"] or "").strip().lower()
        if action_token == "run.cancel":
            seen_invocations.add(invocation_token)
            continue
        status_token = str(details.get("status") or "").strip().lower()
        if status_token not in {"running", "queued"}:
            seen_invocations.add(invocation_token)
            continue
        run_ids: list[str] = []
        primary = normalize_run_id_token(details.get("run_id"))
        if primary:
            run_ids.append(primary)
        raw_run_ids = details.get("run_ids")
        if isinstance(raw_run_ids, list):
            for item in raw_run_ids:
                token = normalize_run_id_token(item)
                if token:
                    run_ids.append(token)
        run_ids = dedupe_preserve_order(run_ids)
        if not run_ids:
            seen_invocations.add(invocation_token)
            continue
        source_paths: list[str] = []
        raw_submission_paths = details.get("submission_paths")
        if isinstance(raw_submission_paths, list):
            source_paths = [str(item or "").strip() for item in raw_submission_paths]
        if not source_paths:
            raw_solution_paths = details.get("solution_paths")
            if isinstance(raw_solution_paths, list):
                source_paths = [str(item or "").strip() for item in raw_solution_paths]
        pending.append(
            {
                "invocation_id": invocation_token,
                "created_at": str(row["created_at"] or "").strip(),
                "mode": str(details.get("mode") or "").strip(),
                "run_ids": run_ids,
                "source_paths": source_paths,
            }
        )
        seen_invocations.add(invocation_token)
    return pending

def run_source_labels_from_audit(problem_id: int, actor_user_id: int, run_ids: list[str], limit: int=240) -> dict[str, str]:
    targets = {normalize_run_id_token(token) for token in run_ids if normalize_run_id_token(token)}
    if not targets:
        return {}
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT ?\n        ", [int(problem_id), int(actor_user_id), max(40, int(limit))])
    resolved: dict[str, str] = {}
    for row in rows:
        if len(resolved) >= len(targets):
            break
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        raw_run_ids = details.get('run_ids')
        if not isinstance(raw_run_ids, list):
            continue
        ordered_run_ids: list[str] = []
        for raw in raw_run_ids:
            token = normalize_run_id_token(raw)
            if token and token not in ordered_run_ids:
                ordered_run_ids.append(token)
        if not ordered_run_ids:
            continue
        if all((run_token not in targets for run_token in ordered_run_ids)):
            continue
        path_labels: dict[str, str] = {}

        def _assign_paths(raw_paths: object) -> bool:
            if not isinstance(raw_paths, list):
                return False
            values = [str(item or '').strip() for item in raw_paths]
            if len(values) != len(ordered_run_ids):
                return False
            assigned = False
            for idx, run_token in enumerate(ordered_run_ids):
                value = str(values[idx] or '').strip()
                if not value:
                    continue
                path_labels[run_token] = value
                assigned = True
            return assigned

        has_assigned_paths = _assign_paths(details.get('submission_paths'))
        if not has_assigned_paths:
            _assign_paths(details.get('solution_paths'))
        fallback_source_label = str(details.get('source_label') or '').strip()
        fallback_upload_name = str(details.get('upload_filename') or '').strip()
        uploaded = bool(details.get('uploaded'))
        primary_run_id = normalize_run_id_token(details.get('run_id'))
        for run_token in ordered_run_ids:
            if run_token not in targets or run_token in resolved:
                continue
            label = str(path_labels.get(run_token) or '').strip()
            if not label and primary_run_id and run_token == primary_run_id and fallback_source_label:
                label = fallback_source_label
            if not label and len(ordered_run_ids) == 1 and uploaded:
                label = fallback_upload_name or fallback_source_label or 'upload'
            if label:
                resolved[run_token] = label
    return resolved

def run_invocation_scope_run_ids(
    problem_id: int,
    workspace_id: int,
    invocation_id: str,
    actor_user_id: int | None = None,
) -> list[str]:
    requested_token = normalize_run_id_token(invocation_id)
    if not requested_token:
        return []
    safe_invocation_id = requested_token
    scan_limit = max(160, _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS * 8)
    rows = config.db.fetch_all('\n        SELECT id,created_at,length(summary_json) AS summary_len,summary_json\n        FROM runs\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT ?\n        ', [int(problem_id), int(workspace_id), int(scan_limit)])
    member_ids_seen: set[str] = set()
    member_ids_desc: list[str] = []
    declared_run_ids: list[str] = []
    summary_budget_used = 0
    summary_rows_loaded = 0
    for row in rows:
        run_id = normalize_run_id_token(row['id'])
        if not run_id:
            continue
        summary_obj: dict | None = None
        try:
            summary_len = int(row['summary_len'] or 0)
        except Exception:
            summary_len = 0
        should_load_summary = summary_len > 0 and summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT and (summary_budget_used + summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET) and (summary_rows_loaded < _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS)
        if should_load_summary:
            summary_obj = parse_summary_json(row['summary_json'], f'run/invocation/{run_id}')
            summary_budget_used += summary_len
            summary_rows_loaded += 1
        current_invocation = _run_invocation_id_from_summary(summary_obj, '')
        if current_invocation != safe_invocation_id:
            continue
        if run_id not in member_ids_seen:
            member_ids_seen.add(run_id)
            member_ids_desc.append(run_id)
        declared_run_ids.extend(_run_invocation_run_ids_from_summary(summary_obj))
    ordered: list[str] = []
    for token in dedupe_preserve_order(declared_run_ids):
        if token and token not in ordered:
            ordered.append(token)
    for token in reversed(member_ids_desc):
        if token and token not in ordered:
            ordered.append(token)
    if ordered:
        return ordered
    if actor_user_id is None:
        return []
    try:
        actor_id = int(actor_user_id)
    except Exception:
        return []
    if actor_id <= 0:
        return []
    try:
        audit_maps = _run_invocation_maps_from_audit(
            int(problem_id),
            actor_id,
            limit=max(160, int(scan_limit)),
        )
    except Exception:
        return []
    audit_run_ids = audit_maps.get(safe_invocation_id)
    if isinstance(audit_run_ids, list):
        audit_ordered = dedupe_preserve_order(
            [normalize_run_id_token(token) for token in audit_run_ids if normalize_run_id_token(token)]
        )
        if audit_ordered:
            return audit_ordered
    return []

def _run_expected_behavior_from_summary(summary: dict | None, source: str) -> str:
    block = _run_invocation_block(summary)
    expected = normalize_expected_behavior(str(block.get('expected_behavior') or 'unknown')) if isinstance(block, dict) else 'unknown'
    if expected != 'unknown':
        return expected
    safe_solution = normalize_optional_component_source_path_safe(source, 'solutions', 'solution path')
    if safe_solution:
        inferred = infer_expected_behavior_from_name(safe_solution)
        if inferred != 'unknown':
            return inferred
    return 'unknown'

def _wall_time_slack_sec_for_mode(mode: object) -> int:
    token = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    if token == 'interactive':
        return coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_INTERACTIVE_SEC', 15), 15, 0, 300)
    if token == 'multi-pass':
        return coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_MULTI_PASS_SEC', 15), 15, 0, 300)
    return coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_PASS_FAIL_SEC', 1), 1, 0, 300)

def _effective_run_timeout_ms(time_limit_ms: int, *, mode: object='pass-fail') -> int:
    tl = max(1, int(time_limit_ms))
    slack_ms = _wall_time_slack_sec_for_mode(mode) * 1000
    return max(1, tl * 2 + slack_ms)

def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    limits = summary.get('limits')
    if isinstance(limits, dict):
        try:
            wall_ms = int(limits.get('wall_ms') or 0)
            if wall_ms > 0:
                return wall_ms
        except Exception:
            pass
        try:
            cpu_ms = int(limits.get('cpu_ms') or 0)
            if cpu_ms > 0:
                return cpu_ms
        except Exception:
            pass
    run_cfg = summary.get('run_config')
    if not isinstance(run_cfg, dict):
        return 0
    mode = normalize_problem_mode(run_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    try:
        time_limit_ms = int(run_cfg.get('time_limit_ms') or 0)
        if time_limit_ms > 0:
            return _effective_run_timeout_ms(time_limit_ms, mode=mode)
    except Exception:
        pass
    try:
        run_timeout_ms = int(run_cfg.get('run_timeout_ms') or 0)
        if run_timeout_ms > 0:
            return run_timeout_ms
    except Exception:
        pass
    try:
        run_timeout_sec = int(run_cfg.get('run_timeout_sec') or 0)
        if run_timeout_sec > 0:
            return run_timeout_sec * 1000
    except Exception:
        pass
    return 0

def _run_cell_kind(verdict: str, expected_behavior: str) -> str:
    short = _run_verdict_short(verdict)
    if short in {'', '-', '--'}:
        return 'neutral'
    if short in {'FL', 'CE'}:
        return 'fail'
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    allowed_set = set(allowed_codes)
    required_set = set(required_codes)
    if short not in allowed_set:
        return 'fail'
    if not required_set:
        return 'ok' if short == 'AC' else 'neutral'
    if short in required_set:
        return 'ok' if short == 'AC' else 'expected-nonac'
    return 'neutral'

def _run_verdict_short(verdict: str) -> str:
    return run_verdict_short(verdict)

def _run_error_display(error: str) -> str:
    return run_error_display(error)

def _run_actual_short(run_status: str, summary: dict | None) -> str:
    return run_actual_short(run_status, summary)

def _run_actual_display(run_status: str, summary: dict | None) -> str:
    return run_actual_display(run_status, summary)

def _run_memory_mb_text(memory_kb: int) -> str:
    return run_memory_mb_text(memory_kb)

def _run_cpu_wall_ms_text(cpu_ms: int, wall_ms: int) -> str:
    return run_cpu_wall_ms_text(cpu_ms, wall_ms)

def _latest_iso_timestamp(values: list[str]) -> str:
    best_raw = ''
    best_ts = None
    for raw in values:
        token = str(raw or '').strip()
        if not token:
            continue
        parsed = parse_iso_utc(token)
        if parsed is None:
            if not best_raw:
                best_raw = token
            continue
        if best_ts is None or parsed > best_ts:
            best_ts = parsed
            best_raw = token
    return best_raw

def _earliest_iso_timestamp(values: list[str]) -> str:
    best_raw = ''
    best_ts = None
    for raw in values:
        token = str(raw or '').strip()
        if not token:
            continue
        parsed = parse_iso_utc(token)
        if parsed is None:
            if not best_raw:
                best_raw = token
            continue
        if best_ts is None or parsed < best_ts:
            best_ts = parsed
            best_raw = token
    return best_raw

def _run_test_sort_key(test_name: str) -> tuple[int, int, str]:
    text = str(test_name or '').strip()
    stem = Path(text).stem if text else ''
    match = re.search('\\d+', stem or text)
    if match:
        try:
            return (0, int(match.group(0)), text)
        except Exception:
            pass
    return (1, 0, text)

def _run_test_answer_name(test_name: str) -> str:
    name = str(test_name or '').strip()
    if not name:
        return ''
    stem = Path(name).stem
    if not stem:
        stem = name
    return f'{stem}.ans'


def _run_list_hydrate_invocation_members(problem_id: int, workspace_id: int, members: list[dict[str, object]]) -> bool:
    candidate_ids: list[str] = []
    for item in members:
        if not isinstance(item, dict):
            continue
        if bool(item.get('summary_loaded')):
            continue
        run_id = normalize_run_id_token(item.get('id'))
        if run_id:
            candidate_ids.append(run_id)
    target_ids = dedupe_preserve_order(candidate_ids)
    if not target_ids:
        return False
    placeholders = ','.join(('?' for _ in target_ids))
    rows = config.db.fetch_all(
        f'SELECT id,status,summary_json FROM runs WHERE problem_id=? AND workspace_id=? AND id IN ({placeholders})',
        [int(problem_id), int(workspace_id), *target_ids],
    )
    row_by_id: dict[str, dict] = {}
    for row in rows:
        run_id = str(row['id'] or '').strip()
        if run_id:
            row_by_id[run_id] = row
    changed = False
    for item in members:
        if not isinstance(item, dict):
            continue
        if bool(item.get('summary_loaded')):
            continue
        run_id = normalize_run_id_token(item.get('id'))
        if not run_id:
            item['summary_loaded'] = True
            continue
        row = row_by_id.get(run_id)
        if row is None:
            item['summary_loaded'] = True
            continue
        summary = parse_summary_json(row['summary_json'], f'run/list/hydrate/{run_id}')
        status_text = str(row['status'] or item.get('status') or '').strip().lower() or 'unknown'
        source = _run_source_from_summary(summary)
        if not source:
            source = str(item.get('source') or '').strip()
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, status_text, summary)
        tests_total = _run_test_count_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        new_values = {
            'source': source,
            'status': status_text,
            'tests_total': tests_total,
            'expected_behavior': expected_behavior,
            'expected_behavior_label': expected_behavior_label(expected_behavior),
            'matched': bool(matched),
            'completed': bool(completed),
            'passed_all_tests': bool(observed_pass),
            'reason': str(reason or ''),
            'invocation_source': invocation_source,
            'is_main_correct_run': _run_is_main_correct_invocation_source(invocation_source),
            'summary_loaded': True,
        }
        for key, value in new_values.items():
            if item.get(key) != value:
                item[key] = value
                changed = True
    return changed

def run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int=40, actor_user_id: int | None=None) -> list[dict]:
    limit_cap = max(1, int(limit))
    fetch_limit = limit_cap * _C.RUN_INVOCATION_LIST_SCAN_FACTOR
    rows = config.db.fetch_all('\n        SELECT id,build_id,mode,status,created_at,length(summary_json) AS summary_len\n        FROM runs\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT ?\n        ', [int(problem_id), int(workspace_id), fetch_limit])
    summary_budget_used = 0
    summary_rows_loaded = 0
    audit_invocation_runs_map: dict[str, list[str]] = {}
    if actor_user_id is not None:
        try:
            audit_invocation_runs_map = _run_invocation_maps_from_audit(int(problem_id), int(actor_user_id), limit=max(160, fetch_limit * 4))
        except Exception:
            audit_invocation_runs_map = {}
    groups_order: list[str] = []
    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        run_id = str(row['id'] or '').strip()
        if not run_id:
            continue
        summary: dict | None = None
        try:
            summary_len = int(row['summary_len'] or 0)
        except Exception:
            summary_len = 0
        should_load_summary = summary_len > 0 and summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT and (summary_budget_used + summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET) and (summary_rows_loaded < _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS)
        if should_load_summary:
            summary_row = config.db.fetch_one('SELECT summary_json FROM runs WHERE id=?', [run_id])
            summary_raw = str(summary_row['summary_json'] or '') if summary_row is not None else ''
            if summary_raw:
                summary = parse_summary_json(summary_raw, f'run/list/{run_id}')
                summary_budget_used += len(summary_raw)
                summary_rows_loaded += 1
        source = _run_source_from_summary(summary)
        status_text = str(row['status'] or '').strip().lower() or 'unknown'
        invocation_id = _run_invocation_id_from_summary(summary, run_id) or run_id
        declared_from_audit = audit_invocation_runs_map.get(invocation_id) or []
        declared_from_summary = _run_invocation_run_ids_from_summary(summary)
        declared_ids = dedupe_preserve_order([*declared_from_audit, *declared_from_summary])
        if invocation_id not in groups:
            groups_order.append(invocation_id)
            groups[invocation_id] = {'id': invocation_id, 'created_at': row['created_at'], 'mode': str(row['mode'] or '').strip(), 'members': [], 'member_ids': set(), 'declared_run_ids': declared_ids}
        group = groups.get(invocation_id)
        if group is None:
            continue
        group_created_at = _earliest_iso_timestamp([str(group.get('created_at') or '').strip(), str(row['created_at'] or '').strip()])
        if group_created_at:
            group['created_at'] = group_created_at
        existing_declared_raw = group.get('declared_run_ids')
        existing_declared = existing_declared_raw if isinstance(existing_declared_raw, list) else []
        group['declared_run_ids'] = dedupe_preserve_order([*existing_declared, *declared_ids])
        member_ids = group.get('member_ids')
        if isinstance(member_ids, set) and run_id in member_ids:
            continue
        if isinstance(member_ids, set):
            member_ids.add(run_id)
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, status_text, summary)
        tests_total = _run_test_count_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        if isinstance(group.get('members'), list):
            group['members'].append({'id': run_id, 'source': source, 'status': status_text, 'tests_total': tests_total, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or ''), 'invocation_source': invocation_source, 'is_main_correct_run': _run_is_main_correct_invocation_source(invocation_source), 'summary_loaded': bool(summary is not None)})
    if actor_user_id is not None:
        pending_invocations = _run_pending_invocations_from_audit(
            int(problem_id),
            int(actor_user_id),
            limit=max(160, fetch_limit * 4),
        )
        for pending in reversed(pending_invocations):
            invocation_id = normalize_run_id_token(pending.get("invocation_id"))
            if not invocation_id or invocation_id in groups:
                continue
            run_ids_raw = pending.get("run_ids")
            run_ids = run_ids_raw if isinstance(run_ids_raw, list) else []
            source_paths_raw = pending.get("source_paths")
            source_paths = source_paths_raw if isinstance(source_paths_raw, list) else []
            members: list[dict[str, object]] = []
            for idx_run, run_token in enumerate(run_ids):
                safe_run_token = normalize_run_id_token(run_token)
                if not safe_run_token:
                    continue
                source_label = ""
                if idx_run < len(source_paths):
                    source_label = str(source_paths[idx_run] or "").strip()
                members.append(
                    {
                        "id": safe_run_token,
                        "source": source_label,
                        "status": "running",
                        "tests_total": 0,
                        "expected_behavior": "unknown",
                        "expected_behavior_label": expected_behavior_label("unknown"),
                        "matched": False,
                        "completed": False,
                        "passed_all_tests": False,
                        "reason": "pending",
                        "invocation_source": "",
                        "is_main_correct_run": False,
                        "summary_loaded": False,
                    }
                )
            if not members:
                continue
            groups_order.insert(0, invocation_id)
            groups[invocation_id] = {
                "id": invocation_id,
                "created_at": str(pending.get("created_at") or "").strip(),
                "mode": str(pending.get("mode") or "").strip(),
                "members": members,
                "member_ids": {str(item.get("id") or "") for item in members if str(item.get("id") or "").strip()},
                "declared_run_ids": [str(item.get("id") or "") for item in members if str(item.get("id") or "").strip()],
            }
    result: list[dict] = []
    for invocation_id in groups_order:
        group = groups.get(invocation_id) or {}
        members_raw = group.get('members')
        members = members_raw if isinstance(members_raw, list) else []
        members_by_id = {str(item.get('id') or ''): item for item in members if isinstance(item, dict)}
        declared_ids_raw = group.get('declared_run_ids')
        declared_ids = declared_ids_raw if isinstance(declared_ids_raw, list) else []
        ordered_member_ids: list[str] = []
        for token in declared_ids:
            run_token = normalize_run_id_token(token)
            if run_token and run_token not in ordered_member_ids:
                ordered_member_ids.append(run_token)
        for item in members:
            if not isinstance(item, dict):
                continue
            token = normalize_run_id_token(item.get('id'))
            if token and token not in ordered_member_ids:
                ordered_member_ids.append(token)
        ordered_members: list[dict[str, object]] = []
        for token in ordered_member_ids:
            existing = members_by_id.get(token)
            if isinstance(existing, dict):
                ordered_members.append(existing)
                continue
            ordered_members.append({'id': token, 'source': '', 'status': 'running', 'tests_total': 0, 'expected_behavior': 'unknown', 'expected_behavior_label': expected_behavior_label('unknown'), 'matched': False, 'completed': False, 'passed_all_tests': False, 'reason': 'pending'})
        if not ordered_members:
            continue
        invocation_sources = {
            str(item.get('invocation_source') or '').strip().lower()
            for item in ordered_members
            if isinstance(item, dict) and str(item.get('invocation_source') or '').strip()
        }
        safe_invocation_hint = normalize_run_id_token(invocation_id)
        if invocation_sources and invocation_sources.issubset({'build.generate-input', 'build.solve'}):
            continue
        if (not invocation_sources) and (safe_invocation_hint.startswith('inv-buildgen-') or safe_invocation_hint.startswith('inv-buildsolve-')):
            continue
        is_main_correct_run = bool(invocation_sources) and invocation_sources.issubset({'build.solve'})
        if (not is_main_correct_run) and safe_invocation_hint.startswith('inv-buildsolve-'):
            is_main_correct_run = True
        status_summary = _run_invocation_status_summary(ordered_members)
        status_text = str(status_summary['status'])
        has_running = bool(status_summary.get('has_running'))
        if (not has_running) and status_text == 'failed':
            if _run_list_hydrate_invocation_members(problem_id, workspace_id, ordered_members):
                status_summary = _run_invocation_status_summary(ordered_members)
                status_text = str(status_summary['status'])
                has_running = bool(status_summary.get('has_running'))
        running_statuses = {'running', 'queued', 'pending'}
        completed_members = [
            item
            for item in ordered_members
            if str(item.get('status') or '').strip().lower() not in running_statuses
        ]
        completed_count = len(completed_members)
        matched_completed_count = sum((1 for item in completed_members if bool(item.get('matched'))))
        matched_count = int(status_summary['matched_count'])
        total_count = int(status_summary['total_count'])
        is_failed = bool(status_summary['is_failed'])
        test_totals = [int(item.get('tests_total') or 0) for item in ordered_members if int(item.get('tests_total') or 0) > 0]
        tests_label = 'tests: -'
        tests_total = 0
        if has_running:
            if test_totals:
                tests_total = max(test_totals)
                tests_label = f'tests: up to {tests_total} (in progress)'
            else:
                tests_label = 'tests: in progress'
        elif test_totals:
            tests_total = max(test_totals)
            min_total = min(test_totals)
            max_total = max(test_totals)
            if min_total == max_total:
                tests_label = f'tests: 1-{max_total} (all)'
            else:
                tests_label = f'tests: {min_total}-{max_total} (varied)'
        source_items = [str(item.get('source') or '').strip() for item in ordered_members if str(item.get('source') or '').strip()]
        source_display = '-'
        if source_items:
            shown = source_items[:2]
            extra = len(source_items) - len(shown)
            source_display = ', '.join(shown)
            if extra > 0:
                source_display += f', +{extra}'
        rejudge_context = _run_rejudge_context_for_entries(ordered_members, workspace)
        rerun_paths = rejudge_context.get('paths')
        rerun_query = str(rejudge_context.get('query') or '')
        rerun_unavailable_reason = str(rejudge_context.get('unavailable_reason') or '')
        if not isinstance(rerun_paths, list):
            rerun_paths = []
        result.append({'index': 0, 'id': invocation_id, 'run_ids': ordered_member_ids, 'run_ids_csv': ','.join(ordered_member_ids), 'run_count': total_count, 'build_id': '', 'mode': str(group.get('mode') or ''), 'status': status_text, 'status_upper': str(status_summary['status_upper']), 'created_at': group.get('created_at'), 'source_display': source_display, 'tests_label': tests_label, 'tests_total': tests_total, 'matched_count': matched_count, 'matched_completed_count': matched_completed_count, 'completed_count': completed_count, 'has_running': has_running, 'is_failed': is_failed, 'is_main_correct_run': bool(is_main_correct_run), 'rerun_solution_paths': rerun_paths, 'rerun_solution_query': rerun_query, 'rerun_unavailable_reason': rerun_unavailable_reason})
    def _invocation_run_time_sort_key(item: dict[str, object]) -> tuple[int, float, str]:
        raw = str(item.get('created_at') or '').strip()
        parsed = parse_iso_utc(raw)
        if parsed is None:
            return (0, -1.0, raw)
        return (1, float(parsed.timestamp()), raw)

    result.sort(key=_invocation_run_time_sort_key, reverse=True)
    trimmed = result[:limit_cap]
    for idx, row in enumerate(trimmed, start=1):
        row['index'] = idx
    return trimmed



