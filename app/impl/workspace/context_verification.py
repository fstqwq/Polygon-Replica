from __future__ import annotations
import os
import re
from pathlib import Path
from typing import cast
from app.impl.runtime.config import config
from app.main_util import preserve_error_text
from app.service.platform.hashing import quick_fp_digest
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.summary import verification_run_ids
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
    if raw is None:
        return ""
    token = raw.strip()
    if not token:
        return ""
    if not re.fullmatch("[A-Za-z0-9._-]{1,80}", token):
        return ""
    return token
def _source_basename_label(path: str) -> str:
    raw = path.strip()
    if not raw:
        return ""
    name = Path(raw).name
    return name or raw

def _verification_has_stage_results(verification_id: str) -> bool:
    if not verification_id:
        return False
    stage_results = cast(dict[str, object] | None, config.verification_service.verification_summary(verification_id).get("stage_results"))
    return bool(stage_results)


def latest_workspace_stage_verification(problem_id: int, workspace_id: int, *, ok_only: bool=False):
    rows = config.verification_service.latest_workspace_stage_rows(
        int(problem_id),
        int(workspace_id),
        limit=40,
        ok_only=bool(ok_only),
    )
    fallback: dict | None = None
    for row in rows:
        if fallback is None:
            fallback = row
        if _verification_has_stage_results(row["id"]):
            return row
    return fallback


def latest_workspace_committed_stage_verification(problem_id: int, workspace_id: int, head_commit: str, *, ok_only: bool=False):
    commit = head_commit
    if not commit:
        return None
    rows = config.verification_service.latest_workspace_committed_stage_rows(
        int(problem_id),
        int(workspace_id),
        source_commit=commit,
        source_ref=commit,
        limit=40,
        ok_only=bool(ok_only),
    )
    fallback: dict | None = None
    for row in rows:
        if fallback is None:
            fallback = row
        if _verification_has_stage_results(row["id"]):
            return row
    return fallback

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
def _stat_mtime_ns(stat_obj: os.stat_result) -> int:
    return int(getattr(stat_obj, 'st_mtime_ns', int(float(stat_obj.st_mtime) * 1_000_000_000)))


def _verification_sources_signature_from_targets(workspace: Path, file_targets: tuple[str, ...], dir_targets: tuple[str, ...]) -> str:
    entries: list[dict[str, object]] = []
    try:
        workspace_resolved = workspace.resolve()
    except OSError:
        workspace_resolved = workspace

    def _is_within_workspace(path: Path) -> bool:
        return workspace_resolved == path or workspace_resolved in path.parents

    def _safe_file(rel_path: str) -> Path | None:
        target = workspace / rel_path
        try:
            if target.is_symlink() or not target.exists() or not target.is_file():
                return None
            resolved = target.resolve()
        except OSError:
            return None
        if not _is_within_workspace(resolved):
            return None
        return target

    def _hash_file(rel_path: str, target: Path | None) -> None:
        if target is None:
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'missing'})
            return
        try:
            stat_obj = target.stat()
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': _stat_mtime_ns(stat_obj)})
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
        if not _is_within_workspace(root_resolved):
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
            if not _is_within_workspace(dir_root_resolved):
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
                if not _is_within_workspace(path_resolved):
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
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': _stat_mtime_ns(stat_obj)})
            except OSError:
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'unreadable'})

    for rel_path in file_targets:
        _hash_file(rel_path, _safe_file(rel_path))
    for rel_dir in dir_targets:
        _hash_dir(rel_dir)
    return quick_fp_digest(entries, schema='verification-signature')

def _verification_sources_signature_details(workspace: Path) -> dict[str, str]:
    details: dict[str, str] = {}
    for label, file_targets, dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
        details[label] = _verification_sources_signature_from_targets(workspace, file_targets, dir_targets)
    return details


def _verification_sources_signature(workspace: Path) -> str:
    return _verification_sources_signature_from_targets(
        workspace,
        _VERIFICATION_SIGNATURE_FILE_TARGETS,
        _VERIFICATION_SIGNATURE_DIR_TARGETS,
    )

def _verification_run_passed(run_status: str, summary: dict[str, object] | None) -> bool:
    if run_status != 'ok' or summary is None:
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests') or []
    if not tests:
        return False
    for row in tests:
        if row.get("verdict") != 'OK':
            return False
    return True

def _verification_run_completed(run_status: str, summary: dict[str, object] | None) -> bool:
    if run_status != 'ok' or summary is None:
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests') or []
    return bool(tests)


def _status_codes(codes: list[str] | tuple[str, ...]) -> list[str]:
    return dedupe_preserve_order([token for token in codes if token])


def _expected_status_rule(expected_behavior: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rule = _EXPECTED_STATUS_RULES.get(normalize_expected_behavior(expected_behavior), _EXPECTED_STATUS_RULES["unknown"])
    return (tuple(_status_codes(rule.get("required", ()))), tuple(_status_codes(rule.get("allowed", ()))))

def _status_codes_display(codes: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(_status_codes(codes)) + "]"


def _status_codes_join(codes: list[str] | tuple[str, ...]) -> str:
    unique = _status_codes(codes)
    return "/".join(unique) if unique else "--"


def _status_rule_expected_display(expected_behavior: str) -> str:
    required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    display_codes = _status_codes(required_codes if required_codes else allowed_codes)
    if not display_codes:
        return "--"
    all_codes = _status_codes(("AC", "WA", "TL", "RE", "CE"))
    if display_codes == all_codes:
        return "any"
    missing_codes = [code for code in all_codes if code not in display_codes]
    if len(display_codes) == len(all_codes) - 1 and len(missing_codes) == 1:
        return f"not {missing_codes[0]}"
    return "/".join(display_codes)


def _run_observed_status_codes(run_status: str, summary: dict | None) -> list[str]:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return failed_codes
    token = run_actual_short(run_status, summary)
    if token in {"", "-", "--"}:
        return []
    return [token]


def _status_rule_expectation_display(expected_behavior: str) -> str:
    required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}"
    )


def _status_rule_display(expected_behavior: str, run_status: str, summary: dict | None) -> str:
    required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    observed_codes = _run_observed_status_codes(run_status, summary)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}, "
        f"got={_status_codes_display(observed_codes)}"
    )


def _status_rule_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, str]:
    required_codes, allowed_codes = _expected_status_rule(expected_behavior)
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
    if run_status in {'running', 'queued', 'pending'}:
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

def _verification_solution_failure_hint(source_path: str, reason: str, error_text: str = "") -> str:
    source_label = _source_basename_label(source_path)
    if not source_label:
        source_label = 'solution'
    rich_error = preserve_error_text(error_text, max_chars=1600, max_lines=24)
    if reason and rich_error:
        detail = f'{reason}: {rich_error}'
    elif reason:
        detail = reason
    elif rich_error:
        detail = rich_error
    else:
        detail = 'verification mismatch'
    return f'{source_label}: {detail}'

def _verification_first_unmatched_hint(solutions: list[dict[str, object]] | None) -> str:
    if not solutions:
        return ''
    for item in solutions:
        if bool(item.get('matched')):
            continue
        source_path = item['source_path']
        reason = item['reason']
        error_text = item['error']
        hint = _verification_solution_failure_hint(source_path, reason, error_text)
        if hint:
            return hint
    return ''

def _verification_stale_reason(changed_components: list[str], *, head_changed: bool, dirty_changed: bool) -> str:
    parts: list[str] = [name for name in changed_components if name]
    if not parts:
        if head_changed:
            parts.append('workspace revision')
        if dirty_changed:
            parts.append('working copy')
    if not parts:
        return 'changed: verification inputs'
    return 'changed: ' + ', '.join(parts)


def _changed_signature_components(recorded_details: dict[str, str], current_details: dict[str, str]) -> list[str]:
    if not recorded_details or not current_details:
        return []
    return [
        label
        for label, _file_targets, _dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS
        if recorded_details.get(label, '') != current_details.get(label, '')
    ]

def _verification_status_context(
    problem_id: int,
    actor_user_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    workspace_path: Path | str | None=None,
) -> dict[str, object]:
    _ = actor_user_id
    rows = config.verification_service.latest_workspace_stage_rows(
        int(problem_id),
        int(workspace_id),
        limit=1,
    )
    row = rows[0] if rows else None
    if row is None:
        return {'mode': 'none', 'display': 'none', 'last_status': 'none', 'run_id': '', 'run_ids': '', 'verification_id': '', 'error': '', 'created_at': '', 'stale': False, 'stale_reason': ''}
    verification_id = row['id']
    details = config.verification_service.verification_summary(verification_id)
    status_token = row['status']
    if status_token == 'ok':
        last_status = 'pass'
    elif status_token in {'queued', 'pending', 'running'}:
        last_status = 'running'
    else:
        last_status = 'failed'
    run_ids = dedupe_preserve_order(verification_run_ids(details))
    run_id = run_ids[0] if run_ids else ''
    verification_created_at = row['created_at']
    recorded_signature = cast(str | None, details.get("verification_signature")) or ""
    recorded_signature_details = cast(dict[str, str] | None, details.get('verification_signature_details')) or {}
    current_signature = ''
    current_signature_details: dict[str, str] = {}
    if workspace_path:
        try:
            workspace_obj = Path(workspace_path)
            current_signature = _verification_sources_signature(workspace_obj)
            current_signature_details = _verification_sources_signature_details(workspace_obj)
        except Exception:
            current_signature = ''
            current_signature_details = {}
    recorded_head = cast(str | None, details.get("workspace_head")) or ""
    recorded_dirty = bool(details.get('workspace_dirty'))
    stale = False
    head_changed = False
    dirty_changed = bool(workspace_dirty) != recorded_dirty
    if recorded_signature and current_signature:
        stale = recorded_signature != current_signature
    else:
        if recorded_head and workspace_head and (recorded_head != workspace_head):
            stale = True
            head_changed = True
        if dirty_changed:
            stale = True
    changed_components = _changed_signature_components(recorded_signature_details, current_signature_details) if stale else []
    error_text = cast(str | None, details.get("error")) or ""
    unmatched_hint = _verification_first_unmatched_hint(cast(list[dict[str, object]] | None, details.get('solutions')) or [])
    if unmatched_hint and (last_status == 'failed' or not error_text):
        error_text = unmatched_hint
    mode = 'stale' if stale else last_status
    stale_reason = _verification_stale_reason(changed_components, head_changed=head_changed, dirty_changed=dirty_changed) if stale else ''
    return {
        'mode': mode,
        'display': mode,
        'last_status': last_status,
        'run_id': run_id,
        'run_ids': ','.join(run_ids),
        'verification_id': verification_id,
        'error': error_text,
        'created_at': verification_created_at,
        'stale': stale,
        'stale_reason': stale_reason,
    }
