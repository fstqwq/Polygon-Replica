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

from .context_operation import dedupe_preserve_order, parse_summary_json
from .context_run_detail import normalize_run_id_token
from .context_ui import page_ctx
from .context_verification import (
    latest_workspace_build,
    _verification_solution_match,
)
from .problem_config import normalize_problem_mode
from .run_dispatch import allocate_run_id

_C = config.constants
def _ensure_implicit_build(
    problem: str,
    user: str,
    *,
    ctx: dict | None=None,
    force: bool=False,
    for_verification: bool=False,
) -> tuple[str, bool]:
    local_ctx = ctx or page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(local_ctx['problem']['id'])
    workspace_id = int(local_ctx['workspace']['id'])
    head_commit = str(local_ctx['workspace'].get('head_commit') or '').strip()
    branch = str(local_ctx['workspace'].get('branch') or 'main').strip() or 'main'
    dirty = bool(local_ctx['workspace'].get('dirty'))
    latest_ok = latest_workspace_build(problem_id, workspace_id, ok_only=True)
    if not force and latest_ok is not None:
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
        build_id = config.build_service.run_build(problem, user, verification_pipeline=True)
    else:
        build_id = config.build_service.run_build(problem, user)
    safe_build_id = str(build_id or "").strip()
    if not safe_build_id:
        raise RuntimeError("build failed: build id is missing")
    row = config.db.fetch_one(
        "SELECT status FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
        [safe_build_id, problem_id, workspace_id],
    )
    status = str(row["status"] or "").strip().lower() if row is not None else ""
    if status and status not in {"ok", "failed", "cancelled"}:
        try:
            waited = str(config.build_service._wait_build_terminal_status(safe_build_id, 30.0) or "").strip().lower()
            if waited:
                status = waited
        except Exception:
            pass
    if status == "ok":
        return (safe_build_id, True)
    _failed_test, reason = _parse_build_failure_context(problem_id, workspace_id, safe_build_id)
    if reason:
        raise RuntimeError(f"build failed: {reason}")
    if status:
        raise RuntimeError(f"build failed: build status is {status}")
    raise RuntimeError("build failed: build metadata missing")

def _allocate_run_id() -> str:
    return allocate_run_id()

def allocate_invocation_id() -> str:
    return f'inv-{secrets.token_hex(6)}'

def _parse_build_failure_context(problem_id: int, workspace_id: int, build_id: str) -> tuple[str, str]:
    safe_build_id = str(build_id or '').strip()
    if not safe_build_id:
        return ('', '')
    row = config.db.fetch_one('SELECT status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [safe_build_id, int(problem_id), int(workspace_id)])
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
    build_error = compact_error_text(str(summary_obj.get('error') or ''))
    reason = ''
    if build_error:
        reason = build_error
    elif failed_step and failed_test_raw:
        reason = compact_error_text(f'{failed_step} failed on {failed_test_raw}')
    elif failed_step:
        reason = compact_error_text(f'{failed_step} failed')
    elif status and status != 'ok':
        reason = f'build status is {status}'
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

def _synthesize_failed_run_tests(*, preferred_test: str='', error_text: str='') -> list[dict]:
    test_name = ''
    for raw in [preferred_test, '001.in']:
        token = Path(str(raw or '').strip()).name
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token):
            test_name = token
            break
    if not test_name:
        return []
    feedback = compact_error_text(str(error_text or ''))
    pass_row: dict[str, object] = {'pass': 1, 'verdict': 'FL', 'time_ms': 0, 'memory_kb': 0}
    if feedback:
        pass_row['feedback'] = feedback
    test_row: dict[str, object] = {'test': test_name, 'passes': [pass_row], 'verdict': 'FL', 'sandbox_status': 'fail', 'time_ms': 0, 'memory_kb': 0, 'feedback_files': []}
    if feedback:
        test_row['message'] = feedback
    return [test_row]

def record_async_run_failure(
    problem: str,
    user: str,
    run_id: str,
    *,
    mode: str,
    source_label: str,
    error: str,
    build_id: str,
    invocation_id: str='',
    invocation_run_ids: list[str] | None=None,
    expected_behavior: str='unknown',
    invocation_source: str='run.execute',
    synthesize_failed_tests: bool=True,
    failure_stage: str='',
    execution_skipped: bool=False,
) -> None:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return
    safe_mode = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    safe_source = str(source_label or 'upload').strip() or 'upload'
    safe_error = str(error or 'invocation failed').strip() or 'invocation failed'
    safe_expected = normalize_expected_behavior(expected_behavior)
    safe_invocation_id = str(invocation_id or '').strip()
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', safe_invocation_id):
        safe_invocation_id = ''
    safe_invocation_run_ids: list[str] = []
    raw_run_ids = invocation_run_ids or []
    for raw in raw_run_ids:
        token = str(raw or '').strip()
        if not re.fullmatch('[A-Za-z0-9._-]{1,80}', token):
            continue
        safe_invocation_run_ids.append(token)
    safe_invocation_run_ids = dedupe_preserve_order(safe_invocation_run_ids)
    if safe_run_id not in safe_invocation_run_ids:
        safe_invocation_run_ids.append(safe_run_id)
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    except Exception:
        return
    run_root = config.fs_manager.prepare_run_root(safe_run_id)
    compile_log_name = 'compile.log'
    (run_root / compile_log_name).write_text(safe_error + '\n', encoding='utf-8')
    failed_test, build_reason = _parse_build_failure_context(int(ctx['problem']['id']), int(ctx['workspace']['id']), build_id)
    if not failed_test:
        failed_test = _extract_failed_test_name_from_error(build_reason or safe_error)
    failure_reason = build_reason or safe_error
    safe_failure_stage = str(failure_stage or '').strip().lower()
    tests_payload = _synthesize_failed_run_tests(preferred_test=failed_test, error_text=failure_reason) if bool(synthesize_failed_tests) else []
    summary = {
        'error': safe_error,
        'mode': safe_mode,
        'source': safe_source,
        'tests': tests_payload,
        'tests_total': len(tests_payload),
        'compile_log': compile_log_name,
        'compile_diagnostics': [],
        'toolchain_digest': 'unknown',
        'sandbox_backend': config.invocation_backend_service.active_backend_name(),
        'invocation_backend': config.invocation_backend_service.active_backend_name(),
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
    if safe_invocation_id:
        matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, 'failed', summary)
        invocation_block: dict[str, object] = {'id': safe_invocation_id, 'source': str(invocation_source or 'run.execute').strip() or 'run.execute', 'run_ids': safe_invocation_run_ids, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}
        if execution_skipped:
            invocation_block['execution_skipped'] = True
        summary['invocation'] = invocation_block
    (run_root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    now = now_iso()
    existing = config.db.fetch_one('SELECT id FROM runs WHERE id=?', [safe_run_id])
    safe_build_id = str(build_id or '').strip() or _C.RUN_PLACEHOLDER_BUILD_ID
    build_ref = ''
    build_row = config.db.fetch_one(
        'SELECT build_ref FROM builds WHERE id=? AND problem_id=? AND workspace_id=?',
        [safe_build_id, int(ctx['problem']['id']), int(ctx['workspace']['id'])],
    )
    if build_row is not None:
        build_ref = str(build_row['build_ref'] or '').strip().lower()
    if existing is None:
        config.db.execute('\n            INSERT INTO runs(\n                id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at\n            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)\n            ', [safe_run_id, int(ctx['problem']['id']), int(ctx['workspace']['id']), safe_build_id, build_ref, safe_mode, 'failed', json.dumps(summary), str(run_root), now, now])
        return
    config.db.execute('\n        UPDATE runs\n        SET build_id=?,build_ref=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=?\n        WHERE id=?\n        ', [safe_build_id, build_ref, safe_mode, 'failed', json.dumps(summary), str(run_root), now, safe_run_id])

def _annotate_run_invocation_result(problem_id: int, workspace_id: int, run_id: str, *, invocation_id: str, invocation_run_ids: list[str], expected_behavior: str, invocation_source: str='run.execute') -> dict[str, object]:
    safe_run_id = str(run_id or '').strip()
    safe_invocation_id = normalize_run_id_token(invocation_id) or safe_run_id
    safe_expected = normalize_expected_behavior(expected_behavior)
    safe_run_ids = dedupe_preserve_order([normalize_run_id_token(item) for item in invocation_run_ids if normalize_run_id_token(item)])
    if safe_run_id and safe_run_id not in safe_run_ids:
        safe_run_ids.append(safe_run_id)
    row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [safe_run_id, int(problem_id), int(workspace_id)])
    run_status = str(row['status'] or '').strip().lower() if row is not None else 'missing'
    summary_obj = parse_summary_json(row['summary_json'] if row is not None else None, f'invocation/{safe_run_id}')
    if not isinstance(summary_obj, dict):
        summary_obj = {}
    matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, run_status, summary_obj)
    summary_obj['invocation'] = {'id': safe_invocation_id, 'source': str(invocation_source or 'run.execute').strip() or 'run.execute', 'run_ids': safe_run_ids, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}
    if row is not None:
        config.db.execute('UPDATE runs SET summary_json=? WHERE id=?', [json.dumps(summary_obj), safe_run_id])
    return {'run_id': safe_run_id, 'status': run_status, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}



