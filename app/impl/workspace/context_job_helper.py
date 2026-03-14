from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from app.db import now_iso
from app.impl.auth.public import parse_iso_utc, utc_now
from app.impl.runtime.config import config

from app.main_util import compact_error_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification import (
    VERIFICATION_KIND_VERIFICATION,
    allocate_verification_id as _store_allocate_verification_id,
    load_verification_run,
    load_verification_summary,
    save_verification_run_summary,
    verification_stage_results,
    verification_run_root,
)

from .context_operation import dedupe_preserve_order, parse_summary_json
from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx
from .context_verification import (
    latest_workspace_stage_verification,
    _verification_solution_match,
)
from .problem_config import normalize_problem_mode
from .run_dispatch import allocate_run_id

_C = config.constants
_BACKEND_NAME = config.judgehost_task_service.backend_name()


class VerificationFailureError(RuntimeError):
    def __init__(self, *, verification_id: str, reason: str, status: str = "", failed_test: str = "") -> None:
        safe_reason = str(reason or "").strip() or "verification failed"
        super().__init__(f"verification failed: {safe_reason}")
        self.verification_id = str(verification_id or "").strip()
        self.reason = safe_reason
        self.status = str(status or "").strip().lower()
        self.failed_test = str(failed_test or "").strip()


def _verification_id_for_run(run_id: str, verification_id: str) -> str:
    safe_verification_id = normalize_run_id_token(verification_id)
    if safe_verification_id:
        return safe_verification_id
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        raise RuntimeError('run id is required')
    return f'ver-{safe_run_id}'


def _verification_kind_for_source(verification_source: str) -> str:
    return VERIFICATION_KIND_VERIFICATION


def _ensure_implicit_verification(
    problem: str,
    user: str,
    *,
    ctx: dict | None=None,
    force: bool=False,
    for_verification: bool=False,
    verification_id: str='',
) -> tuple[str, bool]:
    local_ctx = ctx or page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(local_ctx['problem']['id'])
    workspace_id = int(local_ctx['workspace']['id'])
    head_commit = str(local_ctx['workspace'].get('head_commit') or '').strip()
    branch = str(local_ctx['workspace'].get('branch') or 'main').strip() or 'main'
    dirty = bool(local_ctx['workspace'].get('dirty'))
    safe_target_verification_id = normalize_run_id_token(verification_id)
    latest_ok = latest_workspace_stage_verification(problem_id, workspace_id, ok_only=True)
    if (not safe_target_verification_id) and (not force) and latest_ok is not None:
        latest_id = str(latest_ok['id'] or '').strip()
        latest_commit = str(latest_ok['source_commit'] or '').strip()
        latest_ref = str(latest_ok['source_ref'] or '').strip()
        latest_commit_upper = latest_commit.upper()
        same_ref = bool(latest_id) and (latest_ref == branch)
        matches_head = bool(latest_commit) and (
            latest_commit == head_commit or ((not head_commit) and (latest_commit_upper == 'HEAD'))
        )
        if same_ref and (not dirty) and matches_head:
            return (latest_id, False)
        # Dirty v0 workspaces store build.source_commit as "HEAD".
        if same_ref and dirty and (matches_head or (latest_commit_upper == 'HEAD')):
            created_at = parse_iso_utc(str(latest_ok['created_at'] or ''))
            if created_at is not None and (utc_now() - created_at).total_seconds() <= _C.IMPLICIT_BUILD_DIRTY_REUSE_SEC:
                return (latest_id, False)
    if for_verification:
        created_verification_id = config.verification_service.run_verification(
            problem,
            user,
            verification_id=safe_target_verification_id,
        )
    else:
        created_verification_id = config.verification_service.run_verification(problem, user)
    safe_verification_id = str(created_verification_id or "").strip()
    if not safe_verification_id:
        raise RuntimeError("verification failed: verification id is missing")
    row = config.db.fetch_one(
        "SELECT status FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_verification_id, problem_id, workspace_id],
    )
    status = str(row["status"] or "").strip().lower() if row is not None else ""
    if status and status not in {"ok", "failed", "cancelled"}:
        try:
            waited = str(config.verification_service._wait_verification_terminal_status(safe_verification_id, 30.0) or "").strip().lower()
            if waited:
                status = waited
        except Exception:
            pass
    if status == "ok":
        return (safe_verification_id, True)
    if for_verification:
        verification_summary = load_verification_summary(config.db, safe_verification_id)
        if isinstance(verification_summary, dict) and verification_summary:
            stage_results = verification_stage_results(verification_summary)
            generate_stage = stage_results.get("generate_input") if isinstance(stage_results, dict) else None
            solve_stage = stage_results.get("solve_main") if isinstance(stage_results, dict) else None
            generate_status = (
                str(generate_stage.get("status") or "").strip().lower()
                if isinstance(generate_stage, dict)
                else ""
            )
            solve_status = (
                str(solve_stage.get("status") or "").strip().lower()
                if isinstance(solve_stage, dict)
                else ""
            )
            if generate_status == "ok" and solve_status == "ok":
                return (safe_verification_id, True)
    _failed_test, reason = _parse_verification_failure_context(problem_id, workspace_id, safe_verification_id)
    if reason:
        raise VerificationFailureError(
            verification_id=safe_verification_id,
            reason=reason,
            status=status,
            failed_test=_failed_test,
        )
    if status:
        raise VerificationFailureError(
            verification_id=safe_verification_id,
            reason=f"verification status is {status}",
            status=status,
            failed_test=_failed_test,
        )
    raise VerificationFailureError(
        verification_id=safe_verification_id,
        reason="verification metadata missing",
        status=status,
        failed_test=_failed_test,
    )

def _allocate_run_id() -> str:
    return allocate_run_id()

def allocate_verification_id() -> str:
    return _store_allocate_verification_id(config.db)

def _parse_verification_failure_context(problem_id: int, workspace_id: int, verification_id: str) -> tuple[str, str]:
    safe_verification_id = str(verification_id or '').strip()
    if not safe_verification_id:
        return ('', '')
    row = config.db.fetch_one(
        "SELECT status,summary_json FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_verification_id, int(problem_id), int(workspace_id)],
    )
    if row is None:
        return ('', '')
    status = str(row['status'] or '').strip().lower()
    summary_obj: dict = {}
    summary_raw = str(row['summary_json'] or '')
    if summary_raw:
        try:
            parsed = json.loads(summary_raw)
            if isinstance(parsed, dict):
                summary_obj = parsed
        except Exception:
            summary_obj = {}
    failed_test_raw = str(summary_obj.get('failed_test') or '').strip()
    failed_test = ''
    if failed_test_raw:
        candidate = Path(failed_test_raw).name
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', candidate):
            failed_test = candidate
    failed_step = str(summary_obj.get('failed_step') or '').strip()
    artifact_verification_error = compact_error_text(str(summary_obj.get('error') or ''))
    reason = ''
    if artifact_verification_error:
        reason = artifact_verification_error
    elif failed_step and failed_test_raw:
        reason = compact_error_text(f'{failed_step} failed on {failed_test_raw}')
    elif failed_step:
        reason = compact_error_text(f'{failed_step} failed')
    elif status and status != 'ok':
        reason = f'verification status is {status}'
    return (failed_test, reason)

def _extract_failed_test_name_from_error(error_text: str) -> str:
    raw = str(error_text or '')
    match = re.search(r'([A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in)', raw)
    if not match:
        return ''
    token = Path(str(match.group(1) or '')).name
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token):
        return token
    return ''

def _synthesize_failed_run_tests(
    *,
    preferred_test: str='',
    error_text: str='',
    test_names: list[str] | None = None,
) -> list[dict]:
    normalized_tests: list[str] = []
    if isinstance(test_names, list):
        for raw in test_names:
            token = Path(str(raw or '').strip()).name
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token) and token not in normalized_tests:
                normalized_tests.append(token)
    if not normalized_tests:
        for raw in [preferred_test, '001.in']:
            token = Path(str(raw or '').strip()).name
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token):
                normalized_tests.append(token)
                break
    if not normalized_tests:
        return []
    feedback = compact_error_text(str(error_text or ''))
    result_rows: list[dict] = []
    for test_name in normalized_tests:
        pass_row: dict[str, object] = {'pass': 1, 'verdict': 'FL', 'time_ms': 0, 'memory_kb': 0}
        if feedback:
            pass_row['feedback'] = feedback
        test_row: dict[str, object] = {
            'test': test_name,
            'passes': [pass_row],
            'verdict': 'FL',
            'sandbox_status': 'fail',
            'time_ms': 0,
            'memory_kb': 0,
            'feedback_files': [],
        }
        if feedback:
            test_row['message'] = feedback
        result_rows.append(test_row)
    return result_rows

def record_async_run_failure(
    problem: str,
    user: str,
    run_id: str,
    *,
    mode: str,
    source_label: str,
    error: str,
    verification_id: str,
    artifact_verification_id: str='',
    expected_behavior: str='unknown',
    verification_source: str='run.execute',
    synthesize_failed_tests: bool=True,
    failure_stage: str='',
    execution_skipped: bool=False,
    synthesized_test_names: list[str] | None = None,
) -> None:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return
    safe_mode = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    safe_source = str(source_label or 'upload').strip() or 'upload'
    safe_error = str(error or 'verification failed').strip() or 'verification failed'
    safe_expected = normalize_expected_behavior(expected_behavior)
    effective_verification_id = str(verification_id or '').strip()
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', effective_verification_id):
        effective_verification_id = ''
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    except Exception:
        return
    resolved_verification_id = _verification_id_for_run(safe_run_id, effective_verification_id)
    run_root = verification_run_root(config.fs_manager, resolved_verification_id, safe_run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    compile_log_name = 'compile.log'
    compile_log_text = safe_error + '\n'
    (run_root / compile_log_name).write_text(compile_log_text, encoding='utf-8')
    failed_test, build_reason = _parse_verification_failure_context(
        int(ctx['problem']['id']),
        int(ctx['workspace']['id']),
        artifact_verification_id,
    )
    if not failed_test:
        failed_test = _extract_failed_test_name_from_error(build_reason or safe_error)
    failure_reason = build_reason or safe_error
    safe_failure_stage = str(failure_stage or '').strip().lower()
    tests_payload = (
        _synthesize_failed_run_tests(
            preferred_test=failed_test,
            error_text=failure_reason,
            test_names=synthesized_test_names,
        )
        if bool(synthesize_failed_tests)
        else []
    )
    effective_verification_source = str(verification_source or 'run.execute').strip() or 'run.execute'
    summary = {
        'error': safe_error,
        'mode': safe_mode,
        'source': safe_source,
        'tests': tests_payload,
        'tests_total': len(tests_payload),
        'compile_log': compile_log_name,
        'compile_diagnostics': [],
        'toolchain_digest': 'unknown',
        'sandbox_backend': _BACKEND_NAME,
        'verification_backend': _BACKEND_NAME,
        'limits': {},
        'usage': {'tests': len(tests_payload)},
    }
    if safe_failure_stage:
        summary['failure_stage'] = safe_failure_stage
    if execution_skipped:
        summary['execution_skipped'] = True
        if failure_reason:
            summary['execution_skipped_reason'] = failure_reason
        if not safe_failure_stage:
            summary['failure_stage'] = 'build'
    try:
        save_verification_run_summary(
            config.db,
            config.fs_manager,
            verification_id=resolved_verification_id,
            problem_id=int(ctx['problem']['id']),
            workspace_id=int(ctx['workspace']['id']),
            kind=_verification_kind_for_source(effective_verification_source),
            mode=safe_mode,
            verification_source=effective_verification_source,
            source_paths=[safe_source] if safe_source else [],
            run_id=safe_run_id,
            run_status='failed',
            source_label=safe_source,
            expected_behavior=safe_expected,
            run_summary=summary,
            artifact_path=str(run_root),
            error_text=safe_error,
            finished=True,
        )
    except Exception:
        pass
    (run_root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return

def _update_verification_run_match(
    problem_id: int,
    workspace_id: int,
    run_id: str,
    *,
    verification_id: str,
    expected_behavior: str,
    verification_source: str='run.execute',
) -> dict[str, object]:
    safe_run_id = str(run_id or '').strip()
    safe_verification_id = normalize_run_id_token(verification_id) or safe_run_id
    safe_expected = normalize_expected_behavior(expected_behavior)
    resolved_verification_id = _verification_id_for_run(safe_run_id, safe_verification_id)
    run_row = load_verification_run(
        config.db,
        verification_id=resolved_verification_id,
        run_id=safe_run_id,
    )
    summary_obj = {}
    if isinstance(run_row, dict):
        run_summary = run_row.get('summary')
        if isinstance(run_summary, dict):
            summary_obj = dict(run_summary)
    run_status = str(run_row.get('status') or '').strip().lower() if isinstance(run_row, dict) else ''
    if not run_status:
        run_status = 'missing'
    if not summary_obj:
        summary_obj = {}
    matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, run_status, summary_obj)
    try:
        save_verification_run_summary(
            config.db,
            config.fs_manager,
            verification_id=resolved_verification_id,
            problem_id=int(problem_id),
            workspace_id=int(workspace_id),
            kind=_verification_kind_for_source(verification_source),
            mode=str(summary_obj.get('mode') or '').strip() or 'pass-fail',
            verification_source=str(verification_source or 'run.execute').strip() or 'run.execute',
            source_paths=[str(summary_obj.get('source') or '').strip()] if str(summary_obj.get('source') or '').strip() else [],
            run_id=safe_run_id,
            run_status=run_status,
            source_label=str(summary_obj.get('source') or '').strip() or safe_run_id,
            expected_behavior=safe_expected,
            run_summary=summary_obj,
            artifact_path=str(config.fs_manager.prepare_verification_run_root(verification_id, safe_run_id)),
            error_text=str(summary_obj.get('error') or '').strip(),
            finished=run_status not in {'queued', 'pending', 'running'},
        )
    except Exception:
        pass
    return {'run_id': safe_run_id, 'status': run_status, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}


def annotate_verification_run_result(
    problem_id: int,
    workspace_id: int,
    run_id: str,
    *,
    verification_id: str,
    expected_behavior: str,
    verification_source: str='run.execute',
) -> dict[str, object]:
    return _update_verification_run_match(
        problem_id,
        workspace_id,
        run_id,
        verification_id=verification_id,
        expected_behavior=expected_behavior,
        verification_source=verification_source,
    )
