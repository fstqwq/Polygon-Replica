from __future__ import annotations
from collections import OrderedDict
import re
import threading
from pathlib import Path
from typing import TypedDict
from app.impl.runtime.config import config
from app.service.platform.error_text import bounded_display_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.signature import verification_fingerprint, verification_signature
from app.service.verification.types import Kind
from .run_display import run_actual_failed_codes, run_actual_short

_SANITY_STATUS_TOKENS = {"ok", "passed", "pending", "running", "warning", "failed", "skipped"}
_VERIFICATION_FINGERPRINT_CACHE_MAX = 1024


class _FingerprintCacheEntry(TypedDict):
    verification_id: str
    signature: str


_VERIFICATION_FINGERPRINT_CACHE: OrderedDict[
    tuple[int, int, str],
    _FingerprintCacheEntry,
] = OrderedDict()
_VERIFICATION_FINGERPRINT_CACHE_LOCK = threading.RLock()
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


def latest_workspace_source_commit_verification(
    problem_id: int,
    workspace_id: int,
    source_commit: str,
    *,
    ok_only: bool = False,
):
    if not source_commit:
        return None
    return config.verification_service._verification_store.workspace_source_commit_verification_row(
        int(problem_id),
        int(workspace_id),
        source_commit,
        kinds=(Kind.ALL.value, Kind.CUSTOM.value),
        ok_only=bool(ok_only),
    )

def _verification_sources_signature(workspace: Path) -> str:
    return verification_signature(workspace)


def _verification_sources_fingerprint(workspace: Path) -> str:
    return verification_fingerprint(workspace)


def remember_verification_fingerprint(
    problem_id: int,
    workspace_id: int,
    fingerprint: str,
    verification_id: str,
    signature: str = "",
) -> None:
    if not fingerprint or not verification_id:
        return
    key = (int(problem_id), int(workspace_id), fingerprint)
    # Full verification_signature() hashes file contents and remains the correctness boundary.
    # Workspace status is a high-frequency UI path, so repeated full hashes of large tests
    # can become a file-size DoS. This process-local cache only skips full hashing after a
    # stat-based fingerprint is known to map to a recent verification; cache misses still
    # fall back to the full content signature.
    with _VERIFICATION_FINGERPRINT_CACHE_LOCK:
        _VERIFICATION_FINGERPRINT_CACHE[key] = {
            "verification_id": str(verification_id),
            "signature": str(signature or ""),
        }
        _VERIFICATION_FINGERPRINT_CACHE.move_to_end(key)
        while len(_VERIFICATION_FINGERPRINT_CACHE) > _VERIFICATION_FINGERPRINT_CACHE_MAX:
            _VERIFICATION_FINGERPRINT_CACHE.popitem(last=False)


def _cached_verification_for_fingerprint(
    problem_id: int,
    workspace_id: int,
    fingerprint: str,
    rows: list[dict[str, object]],
) -> tuple[dict[str, object] | None, str]:
    if not fingerprint:
        return (None, "")
    key = (int(problem_id), int(workspace_id), fingerprint)
    by_id = {str(row["id"]): row for row in rows}
    with _VERIFICATION_FINGERPRINT_CACHE_LOCK:
        entry = _VERIFICATION_FINGERPRINT_CACHE.get(key)
        if not entry:
            return (None, "")
        _VERIFICATION_FINGERPRINT_CACHE.move_to_end(key)
        row = by_id.get(entry["verification_id"])
        if row is None:
            _VERIFICATION_FINGERPRINT_CACHE.pop(key, None)
            return (None, "")
        return (row, entry["signature"])


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


def _verification_solution_match(
    expected_behavior: str,
    run_status: str,
    summary: dict | None,
) -> tuple[bool, bool, bool, str]:
    if run_status in {'running', 'queued', 'pending'}:
        return (False, False, False, 'running')
    completed = _verification_run_completed(run_status, summary)
    observed_pass = _verification_run_passed(run_status, summary)
    if not completed:
        return (False, False, observed_pass, "")
    rule_matched, rule_reason = _status_rule_match(expected_behavior, run_status, summary)
    matched = bool(rule_matched)
    if matched:
        return (True, True, observed_pass, "")
    if rule_reason:
        return (False, True, observed_pass, rule_reason)
    return (False, True, observed_pass, "verification mismatch")

def _verification_solution_failure_hint(source_path: str, reason: str, error_text: str = "") -> str:
    source_label = _source_basename_label(source_path)
    if not source_label:
        source_label = 'solution'
    rich_error = bounded_display_text(error_text)
    if reason and rich_error:
        detail = f'{reason}: {rich_error}'
    elif reason:
        detail = reason
    elif rich_error:
        detail = rich_error
    else:
        detail = 'verification mismatch'
    return bounded_display_text(f'{source_label}: {detail}')

def _verification_task_run_status(rows: list[VerificationTaskRow]) -> str:
    statuses = {row["status"] for row in rows}
    if statuses & {
        VerificationTaskStore.TASK_PENDING,
        VerificationTaskStore.TASK_QUEUED,
        VerificationTaskStore.TASK_LEASED,
    }:
        return "running"
    if statuses & {VerificationTaskStore.TASK_FAILED, VerificationTaskStore.TASK_CANCELLED}:
        return "failed"
    return "ok"

def _verification_task_rows_failure_hint(verification_id: str) -> str:
    rows = config.verification_task_store.list_rows(verification_id)
    grouped: dict[str, list[VerificationTaskRow]] = {}
    order: list[str] = []
    for row in rows:
        if row["task_kind"] != "solution-run":
            continue
        logical_run_id = row["logical_run_id"]
        if logical_run_id not in grouped:
            grouped[logical_run_id] = []
            order.append(logical_run_id)
        grouped[logical_run_id].append(row)
    for logical_run_id in order:
        current_rows = grouped[logical_run_id]
        first_row = current_rows[0]
        summary: dict[str, object] = {
            "tests": [
                {"test": row["test_name"], "verdict": row["verdict"]}
                for row in current_rows
                if row["verdict"]
            ],
            "error": next((row["error_text"] for row in current_rows if row["error_text"]), ""),
        }
        run_status = _verification_task_run_status(current_rows)
        _matched, completed, _observed_pass, reason = _verification_solution_match(
            first_row["expected_behavior"],
            run_status,
            summary,
        )
        if completed and reason:
            return _verification_solution_failure_hint(first_row["source_path"], reason, "")
        error_text = str(summary["error"])
        if completed and error_text:
            return _verification_solution_failure_hint(first_row["source_path"], "", error_text)
    return ""

def _verification_error_prefers_source_hint(error_text: str) -> bool:
    if not error_text:
        return True
    if error_text in {"verification failed", "solution run did not complete", "verification mismatch"}:
        return True
    return (
        error_text.startswith("required=[")
        and ", allowed=[" in error_text
        and ", got=[" in error_text
    )

def _verification_stale_reason() -> str:
    return "changed: verification inputs"


def _empty_verification_status_context() -> dict[str, object]:
    return {
        'mode': 'none',
        'display': 'none',
        'last_status': 'none',
        'run_id': '',
        'run_ids': '',
        'verification_id': '',
        'error': '',
        'created_at': '',
        'stale': False,
        'stale_reason': '',
    }


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
        limit=40,
        kinds=(Kind.ALL.value,),
    )
    if not rows:
        return _empty_verification_status_context()
    workspace_obj: Path | None = None
    current_fingerprint = ''
    current_signature = ''
    if workspace_path:
        try:
            workspace_obj = Path(workspace_path)
            current_fingerprint = _verification_sources_fingerprint(workspace_obj)
        except Exception:
            workspace_obj = None
            current_fingerprint = ''
    row, current_signature = _cached_verification_for_fingerprint(
        int(problem_id),
        int(workspace_id),
        current_fingerprint,
        [dict(item) for item in rows],
    )
    if row is None and workspace_obj is not None:
        try:
            current_signature = _verification_sources_signature(workspace_obj)
        except Exception:
            current_signature = ''
    if row is None and current_signature:
        row = next((item for item in rows if item["signature"] == current_signature), None)
        if row is not None and current_fingerprint:
            remember_verification_fingerprint(
                int(problem_id),
                int(workspace_id),
                current_fingerprint,
                str(row["id"] or ""),
                current_signature,
            )
    if row is None:
        row = rows[0]
        if current_fingerprint and current_signature:
            remember_verification_fingerprint(
                int(problem_id),
                int(workspace_id),
                current_fingerprint,
                str(row["id"] or ""),
                current_signature,
            )
    verification_id = row['id']
    detail = config.verification_service.verification_detail(verification_id)
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
    stale = bool(recorded_signature and current_signature and (recorded_signature != current_signature))
    record = config.verification_service.verification_record(verification_id) or {}
    sanity_status = str(detail.get("sanity_status") or "").strip().lower()
    if sanity_status not in _SANITY_STATUS_TOKENS:
        sanity_status = "unknown"
    sanity_attention = sanity_status in {"warning", "failed"}
    sanity_error = str(detail.get("error") or "") if sanity_attention else ""
    error_text = str(record.get("fail_reason") or sanity_error or detail.get("error") or "")
    source_error_text = (
        _verification_task_rows_failure_hint(str(verification_id or ""))
        if last_status == "failed" or error_text
        else ""
    )
    if source_error_text and _verification_error_prefers_source_hint(error_text):
        error_text = source_error_text
    mode = 'stale' if stale else last_status
    display = "ok" if mode == "pass" else mode
    if mode == "pass" and sanity_status == "warning":
        display = "ok (has warning)"
    elif mode == "pass" and sanity_status == "failed":
        display = "ok (sanity failed)"
    stale_reason = _verification_stale_reason() if stale else ''
    return {
        'mode': mode,
        'display': display,
        'warn': bool((not stale) and mode == "pass" and sanity_attention),
        'last_status': last_status,
        'run_id': run_id,
        'run_ids': ','.join(run_ids),
        'verification_id': verification_id,
        'error': error_text,
        'created_at': verification_created_at,
        'stale': stale,
        'stale_reason': stale_reason,
    }
