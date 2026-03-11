from __future__ import annotations
import json
import os
import re
from pathlib import Path
from app.impl.auth.public import parse_iso_utc
from app.impl.runtime.config import config
from app.main_util import preserve_error_text
from app.service.platform.hashing import quick_fp_digest
from app.service.problem.solution_metadata import normalize_expected_behavior
from .run_display import run_actual_failed_codes, run_actual_short
_EXPECTED_STATUS_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    # Each expected behavior is evaluated by:
    # 1) required: at least one code from this list must appear.
    # 2) allowed: observed codes must be a subset of this list.
    "accepted": {"required": ("AC",), "allowed": ("AC",)},
    "wrong_answer": {"required": ("WA",), "allowed": ("AC", "WA")},
    "tle_or_correct": {"required": (), "allowed": ("AC", "TL")},
    "tle_or_re": {"required": (), "allowed": ("TL", "RE")},
    "time_limit_exceeded": {"required": ("TL",), "allowed": ("AC", "TL")},
    "run_time_error": {"required": ("RE",), "allowed": ("AC", "RE")},
    "rejected": {"required": ("WA", "TL", "RE", "CE"), "allowed": ("AC", "WA", "TL", "RE", "CE")},
    "unknown": {"required": (), "allowed": ("AC", "WA", "TL", "RE", "CE")},
}
def dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
def normalize_run_id_token(raw: str | None) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    if not re.fullmatch("[A-Za-z0-9._-]{1,80}", token):
        return ""
    return token
def _source_basename_label(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    name = Path(raw).name.strip()
    return name or raw

def latest_workspace_build(problem_id: int, workspace_id: int, *, ok_only: bool=False):
    sql = 'SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? AND workspace_id=?'
    params: list[object] = [problem_id, workspace_id]
    if ok_only:
        sql += " AND status='ok'"
    sql += ' ORDER BY created_at DESC LIMIT 1'
    return config.db.fetch_one(sql, params)

def latest_workspace_committed_build(problem_id: int, workspace_id: int, head_commit: str, *, ok_only: bool=False):
    commit = str(head_commit or '').strip()
    if not commit:
        return None
    sql = 'SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? AND workspace_id=? AND source_commit=? AND source_ref=?'
    params: list[object] = [problem_id, workspace_id, commit, commit]
    if ok_only:
        sql += " AND status='ok'"
    sql += ' ORDER BY created_at DESC LIMIT 1'
    return config.db.fetch_one(sql, params)

def _json_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or '').strip().lower()
    return text in {'1', 'true', 'yes', 'on'}

_VERIFICATION_SIGNATURE_FILE_TARGETS: tuple[str, ...] = (
    'config/problem.json',
    'config/build.json',
    'tests/spec.json',
)
_VERIFICATION_SIGNATURE_DIR_TARGETS: tuple[str, ...] = (
    'generators',
    'validators',
    'checkers',
    'solutions',
    'tests/manual',
    'tests/generator',
)
_VERIFICATION_SIGNATURE_DETAIL_TARGETS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ('general info', ('config/problem.json',), ()),
    ('build config', ('config/build.json',), ()),
    ('generators', (), ('generators',)),
    ('validator', (), ('validators',)),
    ('checker', (), ('checkers',)),
    ('solutions', (), ('solutions',)),
    ('tests', ('tests/spec.json',), ('tests/manual', 'tests/generator')),
)
def _verification_sources_signature_from_targets(workspace: Path, file_targets: tuple[str, ...], dir_targets: tuple[str, ...]) -> str:
    entries: list[dict[str, object]] = []
    try:
        workspace_resolved = workspace.resolve()
    except OSError:
        workspace_resolved = workspace

    def _safe_file(rel_path: str) -> Path | None:
        target = workspace / rel_path
        try:
            if target.is_symlink() or not target.exists() or not target.is_file():
                return None
            resolved = target.resolve()
        except OSError:
            return None
        if workspace_resolved not in resolved.parents and workspace_resolved != resolved:
            return None
        return target

    def _hash_file(rel_path: str, target: Path | None) -> None:
        if target is None:
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'missing'})
            return
        try:
            stat_obj = target.stat()
            mtime_ns = int(getattr(stat_obj, 'st_mtime_ns', int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': mtime_ns})
        except OSError:
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'unreadable'})

    def _hash_dir(rel_dir: str) -> None:
        root = workspace / rel_dir
        try:
            if root.is_symlink() or not root.exists() or not root.is_dir():
                entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'missing'})
                return
            root_resolved = root.resolve()
        except OSError:
            entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'missing'})
            return
        if workspace_resolved not in root_resolved.parents and workspace_resolved != root_resolved:
            entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'invalid'})
            return
        entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'ok'})
        files: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if workspace_resolved not in dir_root_resolved.parents and workspace_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            safe_dirs: list[str] = []
            for name in dirnames:
                child = dir_root / name
                try:
                    if child.is_symlink() or not child.exists() or not child.is_dir():
                        continue
                except OSError:
                    continue
                safe_dirs.append(name)
            dirnames[:] = sorted(safe_dirs)
            for name in sorted(filenames):
                path = dir_root / name
                try:
                    if path.is_symlink() or not path.exists() or not path.is_file():
                        continue
                    path_resolved = path.resolve()
                except OSError:
                    continue
                if workspace_resolved not in path_resolved.parents and workspace_resolved != path_resolved:
                    continue
                try:
                    rel = path.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                files.append((rel, path))
        files.sort(key=lambda item: item[0])
        for rel, path in files:
            try:
                stat_obj = path.stat()
                mtime_ns = int(getattr(stat_obj, 'st_mtime_ns', int(float(stat_obj.st_mtime) * 1_000_000_000)))
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': mtime_ns})
            except OSError:
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'unreadable'})

    for rel_path in file_targets:
        _hash_file(rel_path, _safe_file(rel_path))
    for rel_dir in dir_targets:
        _hash_dir(rel_dir)
    return quick_fp_digest(entries, schema='verification-signature')

def _verification_sources_signature(workspace: Path) -> str:
    return _verification_sources_signature_from_targets(workspace, _VERIFICATION_SIGNATURE_FILE_TARGETS, _VERIFICATION_SIGNATURE_DIR_TARGETS)

def _verification_sources_signature_details(workspace: Path) -> dict[str, str]:
    details: dict[str, str] = {}
    for label, file_targets, dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
        details[label] = _verification_sources_signature_from_targets(workspace, file_targets, dir_targets)
    return details

def _verification_run_passed(run_status: str, summary: dict | None) -> bool:
    if str(run_status or '').strip().lower() != 'ok':
        return False
    if not isinstance(summary, dict):
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests')
    if not isinstance(tests, list) or not tests:
        return False
    for row in tests:
        if not isinstance(row, dict):
            return False
        if str(row.get('verdict') or '').strip().upper() != 'OK':
            return False
    return True

def _verification_run_completed(run_status: str, summary: dict | None) -> bool:
    if str(run_status or '').strip().lower() != 'ok':
        return False
    if not isinstance(summary, dict):
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests')
    if not isinstance(tests, list) or not tests:
        return False
    return True


def _expected_status_rule(expected_behavior: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    normalized = normalize_expected_behavior(expected_behavior)
    rule = _EXPECTED_STATUS_RULES.get(normalized, _EXPECTED_STATUS_RULES["unknown"])
    required_codes = tuple(str(code or "").strip().upper() for code in rule.get("required", ()) if str(code or "").strip())
    allowed_codes = tuple(str(code or "").strip().upper() for code in rule.get("allowed", ()) if str(code or "").strip())
    return (normalized, required_codes, allowed_codes)


def _status_codes_display(codes: list[str] | tuple[str, ...]) -> str:
    unique: list[str] = []
    for raw in codes:
        token = str(raw or "").strip().upper()
        if not token or token in unique:
            continue
        unique.append(token)
    return "[" + ", ".join(unique) + "]"


def _status_codes_join(codes: list[str] | tuple[str, ...]) -> str:
    unique: list[str] = []
    for raw in codes:
        token = str(raw or "").strip().upper()
        if not token or token in unique:
            continue
        unique.append(token)
    return "/".join(unique) if unique else "--"


def _status_rule_expected_display(expected_behavior: str) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    return _status_codes_join(required_codes if required_codes else allowed_codes)


def _run_observed_status_codes(run_status: str, summary: dict | None) -> list[str]:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return [str(code or "").strip().upper() for code in failed_codes if str(code or "").strip()]
    short = run_actual_short(run_status, summary)
    token = str(short or "").strip().upper()
    if token in {"", "-", "--"}:
        return []
    return [token]


def _status_rule_expectation_display(expected_behavior: str) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}"
    )


def _status_rule_display(expected_behavior: str, run_status: str, summary: dict | None) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    observed_codes = _run_observed_status_codes(run_status, summary)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}, "
        f"got={_status_codes_display(tuple(observed_codes))}"
    )


def _status_rule_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, str]:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    observed_codes = _run_observed_status_codes(run_status, summary)
    observed_set = set(observed_codes)
    required_set = set(required_codes)
    allowed_set = set(allowed_codes)
    has_required = True if not required_set else bool(observed_set & required_set)
    only_allowed = observed_set.issubset(allowed_set)
    matched = bool(has_required and only_allowed)
    if matched:
        return (True, "")
    reason = _status_rule_display(expected_behavior, run_status, summary)
    return (False, reason)


def _verification_solution_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, bool, bool, str]:
    status = str(run_status or '').strip().lower()
    if status in {'running', 'queued', 'pending'}:
        return (False, False, False, 'running')
    completed = _verification_run_completed(run_status, summary)
    observed_pass = _verification_run_passed(run_status, summary)
    rule_matched, rule_reason = _status_rule_match(expected_behavior, run_status, summary)
    matched = bool(completed and rule_matched)
    if matched:
        return (True, True, observed_pass, "")
    if rule_reason:
        return (False, completed, observed_pass, rule_reason)
    if not completed:
        return (False, completed, observed_pass, "solution run did not complete")
    return (False, completed, observed_pass, "verification mismatch")

def _verification_solution_failure_hint(source_path: str, reason: str, error_text: str='') -> str:
    source_label = _source_basename_label(source_path) or str(source_path or '').strip() or 'solution'
    reason_text = str(reason or '').strip()
    rich_error = preserve_error_text(str(error_text or ''), max_chars=1600, max_lines=24)
    if reason_text and rich_error:
        detail = f'{reason_text}: {rich_error}'
    elif reason_text:
        detail = reason_text
    elif rich_error:
        detail = rich_error
    else:
        detail = 'verification mismatch'
    return f'{source_label}: {detail}'

def _verification_first_unmatched_hint(solutions: object) -> str:
    if not isinstance(solutions, list):
        return ''
    for item in solutions:
        if not isinstance(item, dict):
            continue
        if bool(item.get('matched')):
            continue
        hint = _verification_solution_failure_hint(
            str(item.get('source_path') or ''),
            str(item.get('reason') or ''),
            str(item.get('error') or ''),
        )
        if hint:
            return hint
    return ''

def _verification_stale_reason(changed_components: list[str], *, head_changed: bool, dirty_changed: bool) -> str:
    parts: list[str] = [str(name or '').strip() for name in changed_components if str(name or '').strip()]
    if not parts:
        if head_changed:
            parts.append('workspace revision')
        if dirty_changed:
            parts.append('working copy')
    if not parts:
        return 'changed: verification inputs'
    return 'changed: ' + ', '.join(parts)

def _verification_status_context(
    problem_id: int,
    actor_user_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    workspace_path: Path | str | None=None,
) -> dict:
    row = config.db.fetch_one("\n        SELECT details_json,created_at\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action='verification.start'\n        ORDER BY created_at DESC\n        LIMIT 1\n        ", [problem_id, actor_user_id])
    if row is None:
        return {'mode': 'none', 'display': 'none', 'last_status': 'none', 'run_id': '', 'run_ids': '', 'build_id': '', 'error': '', 'created_at': '', 'stale': False, 'stale_reason': ''}
    details: dict = {}
    try:
        parsed = json.loads(str(row['details_json'] or '{}'))
        if isinstance(parsed, dict):
            details = parsed
    except Exception:
        details = {}
    last_status = str(details.get('status') or '').strip().lower()
    if last_status not in {'pass', 'failed', 'running'}:
        last_status = 'failed'
    run_id = normalize_run_id_token(details.get('run_id'))
    run_ids: list[str] = []
    raw_run_ids = details.get('run_ids')
    if isinstance(raw_run_ids, list):
        for item in raw_run_ids:
            token = normalize_run_id_token(str(item or ''))
            if token:
                run_ids.append(token)
    elif isinstance(raw_run_ids, str):
        for item in str(raw_run_ids).split(','):
            token = normalize_run_id_token(item)
            if token:
                run_ids.append(token)
    run_ids = dedupe_preserve_order(run_ids)
    if run_id and run_id not in run_ids:
        run_ids.insert(0, run_id)
    if not run_id and run_ids:
        run_id = run_ids[0]
    cancel_reason = ''
    cancel_created_at = ''
    cancel_rows = config.db.fetch_all(
        """
        SELECT details_json,created_at
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='run.cancel'
        ORDER BY created_at DESC
        LIMIT 240
        """,
        [int(problem_id), int(actor_user_id)],
    )
    for cancel_row in cancel_rows:
        cancel_details: dict = {}
        try:
            cancel_payload = json.loads(str(cancel_row['details_json'] or '{}'))
            if isinstance(cancel_payload, dict):
                cancel_details = cancel_payload
        except Exception:
            cancel_details = {}
        cancel_invocation_id = normalize_run_id_token(cancel_details.get('invocation_id'))
        if not cancel_invocation_id:
            continue
        if cancel_invocation_id == normalize_run_id_token(details.get('invocation_id')):
            cancel_reason = str(cancel_details.get('reason') or '').strip() or 'verification cancelled by user'
            cancel_created_at = str(cancel_row['created_at'] or '').strip()
            break
    verification_created_at = str(row['created_at'] or '').strip()
    cancelled_after_start = False
    if cancel_created_at:
        cancel_ts = parse_iso_utc(cancel_created_at)
        verification_ts = parse_iso_utc(verification_created_at)
        if verification_ts is None:
            cancelled_after_start = True
        elif cancel_ts is not None:
            cancelled_after_start = cancel_ts >= verification_ts
        else:
            cancelled_after_start = True
    if cancelled_after_start:
        details['status'] = 'failed'
        if cancel_reason:
            details['error'] = cancel_reason
        last_status = 'failed'
    recorded_signature = str(details.get('verification_signature') or '').strip()
    recorded_signature_details: dict[str, str] = {}
    raw_recorded_details = details.get('verification_signature_details')
    if isinstance(raw_recorded_details, dict):
        for key, value in raw_recorded_details.items():
            label = str(key or '').strip()
            signature = str(value or '').strip()
            if label and signature:
                recorded_signature_details[label] = signature
    current_signature = ''
    current_signature_details: dict[str, str] = {}
    if workspace_path:
        try:
            workspace_obj = Path(str(workspace_path))
            current_signature = _verification_sources_signature(workspace_obj)
            current_signature_details = _verification_sources_signature_details(workspace_obj)
        except Exception:
            current_signature = ''
            current_signature_details = {}
    recorded_head = str(details.get('workspace_head') or '').strip()
    recorded_dirty = _json_truthy(details.get('workspace_dirty'))
    stale = False
    head_changed = False
    dirty_changed = bool(workspace_dirty) != recorded_dirty
    changed_components: list[str] = []
    if recorded_signature and current_signature:
        stale = recorded_signature != current_signature
        if stale and recorded_signature_details and current_signature_details:
            for label, _file_targets, _dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
                if recorded_signature_details.get(label, '') != current_signature_details.get(label, ''):
                    changed_components.append(label)
    else:
        if recorded_head and workspace_head and (recorded_head != workspace_head):
            stale = True
            head_changed = True
        if dirty_changed:
            stale = True
        if stale and recorded_signature_details and current_signature_details:
            for label, _file_targets, _dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
                if recorded_signature_details.get(label, '') != current_signature_details.get(label, ''):
                    changed_components.append(label)
    error_text = str(details.get('error') or '').strip()
    unmatched_hint = _verification_first_unmatched_hint(details.get('solutions'))
    if last_status == 'failed' and unmatched_hint:
        error_text = unmatched_hint
    elif not error_text and unmatched_hint:
        error_text = unmatched_hint
    mode = 'stale' if stale else last_status
    if cancelled_after_start:
        mode = 'failed'
        stale = False
    stale_reason = _verification_stale_reason(changed_components, head_changed=head_changed, dirty_changed=dirty_changed) if stale else ''
    created_at_value = cancel_created_at if cancelled_after_start and cancel_created_at else verification_created_at
    if cancelled_after_start and cancel_reason:
        error_text = cancel_reason
    return {'mode': mode, 'display': mode, 'last_status': last_status, 'run_id': run_id, 'run_ids': ','.join(run_ids), 'build_id': str(details.get('build_id') or '').strip(), 'error': error_text, 'created_at': created_at_value, 'stale': stale, 'stale_reason': stale_reason}


