from __future__ import annotations
import json
import os
from pathlib import Path

from fastapi import HTTPException

from app.impl.runtime.config import config
from app.service.verification import (
    load_verification_record,
    load_verification_summary,
    verification_stage_summary,
)

from .artifact import (
    artifact_root,
)
from .run_display import (
    run_memory_mb_text,
    run_verdict_short,
)
from .run_lifecycle import (
    normalize_verification_step_id,
    run_lifecycle_current_step,
    run_lifecycle_current_step_fields,
    run_lifecycle_status_label,
    verification_failed_build_step_id,
    verification_step_title,
)
from .context_operation import dedupe_preserve_order
from .context_run_detail import (
    normalize_run_id_token,
    normalize_run_test_name_token,
)
from app.service.platform.process import is_canonical_artifact_id

_C = config.constants

def load_verification_detail_snapshot(problem_id: int, verification_id: str) -> dict[str, object]:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return {}
    verification_row = load_verification_record(config.db, safe_verification_id)
    if verification_row is None or int(verification_row['problem_id'] or 0) != int(problem_id):
        return {}
    details = load_verification_summary(config.db, safe_verification_id)
    if not isinstance(details, dict) or not details:
        return {}
    snapshot = dict(details)
    snapshot.setdefault('verification_id', safe_verification_id)
    row_status = str(verification_row['status'] or '').strip()
    if row_status:
        snapshot['status'] = row_status
    snapshot.setdefault('finished_at', str(verification_row['finished_at'] or '').strip())
    return {
        'details': snapshot,
        'created_at': str(verification_row['created_at'] or '').strip(),
    }

def _run_lifecycle_status_label(status: str) -> str:
    return run_lifecycle_status_label(status)

def _run_lifecycle_current_step(steps: list[dict[str, object]]) -> tuple[int, str]:
    return run_lifecycle_current_step(steps)

def _run_lifecycle_current_step_fields(steps: list[dict[str, object]], current_step_index: int) -> tuple[str, str, str]:
    return run_lifecycle_current_step_fields(steps, current_step_index)

def _normalize_verification_step_id(raw: object) -> str:
    return normalize_verification_step_id(raw)

def _verification_step_title(step_id: str) -> str:
    return verification_step_title(step_id)

def _verification_failed_build_step_id(step_hint: str, step_ids: list[str]) -> str:
    return verification_failed_build_step_id(step_hint, step_ids)

def _verification_tests_meta_stats(problem_slug: str, build_id: str) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'total': 0,
        'manual': 0,
        'gen': 0,
        'sample': 0,
    }
    safe_problem = str(problem_slug or '').strip()
    safe_build_id = str(build_id or '').strip()
    if (not safe_problem) or (not is_canonical_artifact_id(safe_build_id)):
        return stats
    try:
        root = artifact_root(safe_problem, safe_build_id)
    except HTTPException:
        return stats
    tests_meta_path = root / 'logs' / 'tests_meta.json'
    try:
        if tests_meta_path.exists() and tests_meta_path.is_file() and (not tests_meta_path.is_symlink()):
            payload = json.loads(tests_meta_path.read_text(encoding='utf-8', errors='replace'))
            if isinstance(payload, list):
                total = 0
                manual = 0
                generated = 0
                sample = 0
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    total += 1
                    kind = str(item.get('kind') or '').strip().lower()
                    if kind == 'manual':
                        manual += 1
                    elif kind == 'gen':
                        generated += 1
                    if bool(item.get('sample')):
                        sample += 1
                stats.update({
                    'loaded': True,
                    'total': total,
                    'manual': manual,
                    'gen': generated,
                    'sample': sample,
                })
                return stats
    except Exception:
        pass
    tests_dir = root / 'tests'
    names: list[str] = []
    try:
        if tests_dir.exists() and tests_dir.is_dir() and (not tests_dir.is_symlink()):
            with os.scandir(tests_dir) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if not _C.RUN_TEST_NAME_RE.fullmatch(name):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    names.append(name)
    except Exception:
        names = []
    if names:
        stats.update({
            'loaded': True,
            'total': len(names),
            'manual': 0,
            'gen': 0,
            'sample': 0,
        })
    return stats

def load_verification_stage_summary(verification_details: dict[str, object], stage_key: str) -> dict[str, object]:
    if not isinstance(verification_details, dict) or not verification_details:
        return {}
    return verification_stage_summary(verification_details, stage_key)

def _verification_validate_stats(verification_details: dict[str, object]) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'truncated': False,
        'total': 0,
        'ok': 0,
        'failed': 0,
        'timed_out': 0,
    }
    summary_obj = load_verification_stage_summary(verification_details, 'generate_input')
    tests_rows = summary_obj.get('tests')
    tests = tests_rows if isinstance(tests_rows, list) else []
    if not tests:
        return stats
    total = 0
    ok_count = 0
    failed_count = 0
    timed_out_count = 0
    for item in tests:
        if not isinstance(item, dict):
            continue
        test_name = str(item.get('test') or '').strip()
        if not _C.RUN_TEST_NAME_RE.fullmatch(test_name):
            continue
        total += 1
        verdict = str(item.get('verdict') or '').strip().upper()
        timed_out = verdict.startswith('TL') or bool(item.get('timed_out'))
        if timed_out:
            timed_out_count += 1
        if verdict in {'OK', 'AC'} and not timed_out:
            ok_count += 1
        else:
            failed_count += 1
    if total <= 0:
        return stats
    stats.update({
        'loaded': True,
        'total': total,
        'ok': ok_count,
        'failed': failed_count,
        'timed_out': timed_out_count,
    })
    return stats


def _verification_output_stats(problem_slug: str, build_id: str) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'total': 0,
        'generated': 0,
    }
    safe_problem = str(problem_slug or '').strip()
    safe_build_id = str(build_id or '').strip()
    if (not safe_problem) or (not is_canonical_artifact_id(safe_build_id)):
        return stats
    try:
        root = artifact_root(safe_problem, safe_build_id)
    except HTTPException:
        return stats
    try:
        if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            return stats
    except OSError:
        return stats
    stats['loaded'] = True
    tests_dir = root / 'tests'
    ans_dir = root / 'ans'
    test_names: set[str] = set()
    try:
        if tests_dir.exists() and tests_dir.is_dir() and (not tests_dir.is_symlink()):
            with os.scandir(tests_dir) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if not _C.RUN_TEST_NAME_RE.fullmatch(name):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    test_names.add(name)
    except Exception:
        test_names = set()
    answered_tests: set[str] = set()
    try:
        if ans_dir.exists() and ans_dir.is_dir() and (not ans_dir.is_symlink()):
            with os.scandir(ans_dir) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if not name.lower().endswith('.ans'):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    stem = Path(name).stem
                    test_name = f'{stem}.in'
                    if _C.RUN_TEST_NAME_RE.fullmatch(test_name):
                        answered_tests.add(test_name)
    except Exception:
        answered_tests = set()
    total = len(test_names)
    if total <= 0 and answered_tests:
        total = len(answered_tests)
    generated = len(answered_tests if not test_names else (answered_tests & test_names))
    stats.update(
        {
            'total': max(0, int(total)),
            'generated': max(0, int(generated)),
        }
    )
    return stats

def _verification_buildsolve_case_progress(build_id: str) -> dict[str, int]:
    safe_build_id = str(build_id or '').strip()
    if not is_canonical_artifact_id(safe_build_id):
        return {'total': 0, 'reported': 0}
    try:
        return dict(config.judgehost_task_service.domjudge_buildsolve_progress(safe_build_id))
    except Exception:
        return {'total': 0, 'reported': 0}

def _verification_selected_tests_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    try:
        selected_count = int(summary.get('selected_tests_count') or 0)
    except Exception:
        selected_count = 0
    if selected_count > 0:
        return selected_count
    selected_tests_raw = summary.get('selected_tests')
    if isinstance(selected_tests_raw, list):
        selected_tests = dedupe_preserve_order(
            [normalize_run_test_name_token(item) for item in selected_tests_raw]
        )
        if selected_tests:
            return len(selected_tests)
    return _run_test_count_from_summary(summary)

def _verification_judgehost_case_progress(run_ids: list[str]) -> dict[str, dict[str, int]]:
    safe_run_ids = dedupe_preserve_order([normalize_run_id_token(item) for item in run_ids if normalize_run_id_token(item)])
    if not safe_run_ids:
        return {}
    try:
        return dict(config.judgehost_task_service.domjudge_case_progress_for_runs(safe_run_ids))
    except Exception:
        return {}


def _run_domjudge_verdict_from_runresult(raw: object) -> str:
    token = str(raw or '').strip().lower()
    mapping = {
        'correct': 'OK',
        'compiler-error': 'CE',
        'timelimit': 'TL',
        'run-error': 'RE',
        'wrong-answer': 'WA',
        'no-output': 'WA',
        'output-limit': 'FL',
        'compare-error': 'FL',
        'internal-error': 'FL',
    }
    return str(mapping.get(token, 'FL'))


def _run_domjudge_case_cells(run_ids: list[str]) -> dict[str, dict[str, dict[str, object]]]:
    safe_run_ids = dedupe_preserve_order([normalize_run_id_token(item) for item in run_ids if normalize_run_id_token(item)])
    if not safe_run_ids:
        return {}
    try:
        rows = list(config.judgehost_task_service.domjudge_case_cells_for_runs(safe_run_ids))
    except Exception:
        return {}
    out: dict[str, dict[str, dict[str, object]]] = {}
    for row in rows:
        run_id = normalize_run_id_token(row['run_id'])
        if not run_id:
            continue
        test_name = normalize_run_test_name_token(row['test_name'])
        if not test_name:
            continue
        status = str(row['status'] or '').strip().lower()
        runresult = str(row['runresult'] or '').strip().lower()
        cpu_sec = 0.0
        runtime_sec = 0.0
        wall_sec = 0.0
        memory_kb = 0
        try:
            cpu_sec = max(0.0, float(row['cpu_sec'] or 0.0))
        except Exception:
            cpu_sec = 0.0
        try:
            runtime_sec = max(0.0, float(row['runtime_sec'] or 0.0))
        except Exception:
            runtime_sec = 0.0
        try:
            wall_sec = max(0.0, float(row['wall_sec'] or 0.0))
        except Exception:
            wall_sec = 0.0
        try:
            memory_kb = max(0, int(row['memory_kb'] or 0))
        except Exception:
            memory_kb = 0

        verdict = ''
        short = '..'
        metrics = 'pending'
        cpu_ms = int(round(max(cpu_sec, runtime_sec) * 1000.0))
        wall_ms = int(round(max(wall_sec, cpu_sec, runtime_sec) * 1000.0))
        reported = status == 'reported'
        if reported:
            verdict = _run_domjudge_verdict_from_runresult(runresult)
            short = run_verdict_short(verdict)
            metrics = f'{cpu_ms}ms/{run_memory_mb_text(memory_kb)}'
        elif status == 'leased':
            metrics = 'running'

        by_run = out.setdefault(run_id, {})
        by_run[test_name] = {
            'test_name': test_name,
            'status': status,
            'reported': bool(reported),
            'verdict': verdict,
            'short': short,
            'time_ms': int(cpu_ms),
            'cpu_ms': int(cpu_ms),
            'wall_ms': int(wall_ms),
            'memory_kb': int(memory_kb),
            'metrics': metrics,
        }
    return out

def _verification_run_test_progress(
    *,
    materialized_columns: list[dict[str, object]],
    run_statuses: list[str],
    run_count: int,
    fallback_tests_per_solution: int,
) -> dict[str, int]:
    safe_run_count = max(0, int(run_count))
    safe_fallback_tests = max(0, int(fallback_tests_per_solution))
    run_ids = [normalize_run_id_token(col.get('id')) for col in materialized_columns if isinstance(col, dict)]
    case_progress_by_run = _verification_judgehost_case_progress([run_id for run_id in run_ids if run_id])
    total_tests = 0
    completed_tests = 0
    running_tests = 0
    started_runs = 0
    for idx, col in enumerate(materialized_columns):
        if not isinstance(col, dict):
            continue
        started_runs += 1
        run_id = normalize_run_id_token(col.get('id'))
        summary = col.get('summary') if isinstance(col.get('summary'), dict) else None
        expected_tests = _verification_selected_tests_count(summary)
        if expected_tests <= 0:
            expected_tests = safe_fallback_tests
        case_progress = case_progress_by_run.get(run_id, {})
        case_total = max(0, int(case_progress.get('total') or 0))
        case_reported = max(0, int(case_progress.get('reported') or 0))
        case_leased = max(0, int(case_progress.get('leased') or 0))
        if case_total > 0:
            expected_tests = max(expected_tests, case_total)
        reported_tests = _run_test_count_from_summary(summary)
        reported_tests = max(reported_tests, case_reported)
        if expected_tests > 0:
            reported_tests = min(expected_tests, max(0, int(reported_tests)))
        else:
            reported_tests = max(0, int(reported_tests))
            expected_tests = reported_tests
        total_tests += expected_tests
        completed_tests += reported_tests
        run_status = str(run_statuses[idx] if idx < len(run_statuses) else '').strip().lower()
        if case_leased > 0:
            running_tests += case_leased
        elif run_status in {'running', 'queued', 'pending'} and expected_tests > reported_tests:
            running_tests += (expected_tests - reported_tests)
    remaining_runs = max(0, safe_run_count - started_runs)
    if remaining_runs > 0 and safe_fallback_tests > 0:
        total_tests += remaining_runs * safe_fallback_tests
    return {
        'total': max(0, int(total_tests)),
        'completed': max(0, int(completed_tests)),
        'running': max(0, int(running_tests)),
    }

def _run_test_count_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if bool(summary.get('execution_skipped')):
        return 0
    tests = summary.get('tests')
    if isinstance(tests, list):
        return len(tests)
    usage = summary.get('usage')
    if isinstance(usage, dict):
        try:
            return max(0, int(usage.get('tests') or 0))
        except Exception:
            return 0
    return 0



