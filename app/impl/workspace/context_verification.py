from __future__ import annotations
import re
from pathlib import Path
from app.impl.runtime.config import config
from app.main_util import preserve_error_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.signature import verification_signature
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

def latest_workspace_verification(problem_id: int, workspace_id: int, *, ok_only: bool=False):
    rows = config.verification_service._verification_store.workspace_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=40,
        ok_only=bool(ok_only),
    )
    return rows[0] if rows else None


def latest_workspace_signature_verification(problem_id: int, workspace_id: int, signature: str, *, ok_only: bool=False):
    safe_signature = signature
    if not safe_signature:
        return None
    rows = config.verification_service._verification_store.workspace_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=40,
        kinds=("all", "custom"),
        ok_only=bool(ok_only),
    )
    rows = [row for row in rows if str(row["signature"] or "") == safe_signature]
    return rows[0] if rows else None

def _verification_sources_signature(workspace: Path) -> str:
    return verification_signature(workspace)

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
        if not bool(item.get('completed')):
            continue
        source_path = str(item.get('source_path') or '')
        reason = str(item.get('reason') or '')
        error_text = str(item.get('error') or '')
        hint = _verification_solution_failure_hint(source_path, reason, error_text)
        if hint:
            return hint
    return ''

def _verification_stale_reason() -> str:
    return "changed: verification inputs"

def _verification_status_context(
    problem_id: int,
    actor_user_id: int,
    workspace_id: int,
    workspace_dirty: bool,
    workspace_path: Path | str | None=None,
) -> dict[str, object]:
    _ = actor_user_id
    rows = config.verification_service._verification_store.workspace_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=1,
    )
    row = rows[0] if rows else None
    if row is None:
        return {'mode': 'none', 'display': 'none', 'last_status': 'none', 'run_id': '', 'run_ids': '', 'verification_id': '', 'error': '', 'created_at': '', 'stale': False, 'stale_reason': ''}
    verification_id = row['id']
    metadata = config.verification_service.verification_metadata(verification_id)
    status_token = row['status']
    if status_token == 'ok':
        last_status = 'pass'
    elif status_token in {'queued', 'pending', 'running'}:
        last_status = 'running'
    else:
        last_status = 'failed'
    run_ids: list[str] = []
    run_id = ''
    verification_created_at = row['created_at']
    recorded_signature = str(row.get("signature") or "")
    current_signature = ''
    if workspace_path:
        try:
            workspace_obj = Path(workspace_path)
            current_signature = _verification_sources_signature(workspace_obj)
        except Exception:
            current_signature = ''
    stale = bool(recorded_signature and current_signature and (recorded_signature != current_signature))
    record = config.verification_service.verification_record(verification_id) or {}
    error_text = str(record.get("fail_reason") or metadata.get("error") or "")
    mode = 'stale' if stale else last_status
    stale_reason = _verification_stale_reason() if stale else ''
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
