from __future__ import annotations

from typing import TypedDict, cast

from app.impl.runtime.config import config
from app.service.verification.store import (
    load_verification_record,
    load_verification_summary,
)

from .run_display import (
    run_memory_mb_text,
    run_verdict_short,
)
from .context_operation import dedupe_preserve_order
from .context_run_detail import (
    normalize_run_id_token,
    normalize_run_test_name_token,
)
_DOMJUDGE_VERDICT_BY_RUNRESULT: dict[str, str] = {
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


VerificationSnapshot = TypedDict(
    "VerificationSnapshot",
    {
        "details": dict[str, object],
        "created_at": str,
    },
)

VerificationSummary = TypedDict(
    "VerificationSummary",
    {
        "tests_total": int,
        "selected_tests_count": int,
        "selected_tests": list[str],
        "execution_skipped": bool,
        "tests": list[dict[str, object]],
        "usage": dict[str, object],
    },
    total=False,
)

JudgehostCaseCellRow = TypedDict(
    "JudgehostCaseCellRow",
    {
        "run_id": str,
        "test_name": str,
        "status": str,
        "runresult": str,
        "cpu_sec": float,
        "runtime_sec": float,
        "wall_sec": float,
        "memory_kb": int,
    },
)

def _normalize_run_ids(raw_run_ids: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw_run_ids:
        run_id = normalize_run_id_token(item)
        if run_id:
            out.append(run_id)
    return dedupe_preserve_order(out)

def load_verification_detail_snapshot(problem_id: int, verification_id: str) -> VerificationSnapshot | dict[str, object]:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return {}
    verification_row = load_verification_record(config.db, safe_verification_id)
    if verification_row is None or int(verification_row['problem_id']) != int(problem_id):
        return {}
    details = load_verification_summary(config.db, safe_verification_id)
    if not details:
        return {}
    snapshot = {
        **details,
        'verification_id': safe_verification_id,
        'finished_at': verification_row['finished_at'],
    }
    snapshot['status'] = verification_row['status']
    return {
        'details': snapshot,
        'created_at': verification_row['created_at'],
    }

def _verification_tests_meta_stats(summary: VerificationSummary | None) -> dict[str, int]:
    if summary is None:
        return {"total": 0}
    try:
        tests_total = int(summary.get("tests_total", 0))
    except Exception:
        tests_total = 0
    if tests_total > 0:
        return {"total": tests_total}
    try:
        selected_count = int(summary.get("selected_tests_count", 0))
    except Exception:
        selected_count = 0
    if selected_count > 0:
        return {"total": selected_count}
    selected_tests_raw = summary.get("selected_tests")
    if selected_tests_raw:
        selected_tests = dedupe_preserve_order(
            [normalize_run_test_name_token(item) for item in selected_tests_raw]
        )
        if selected_tests:
            return {"total": len(selected_tests)}
    return {"total": _run_test_count_from_summary(summary)}

def _verification_judgehost_case_progress(run_ids: list[str]) -> dict[str, dict[str, int]]:
    safe_run_ids = _normalize_run_ids(run_ids)
    if not safe_run_ids:
        return {}
    try:
        return dict(config.judgehost_task_service.domjudge_case_progress_for_runs(safe_run_ids))
    except Exception:
        return {}



def _run_domjudge_case_cells(run_ids: list[str]) -> dict[str, dict[str, dict[str, object]]]:
    safe_run_ids = _normalize_run_ids(run_ids)
    if not safe_run_ids:
        return {}
    try:
        rows = config.judgehost_task_service.domjudge_case_cells_for_runs(safe_run_ids)
    except Exception:
        return {}
    out: dict[str, dict[str, dict[str, object]]] = {}
    for row in cast(list[JudgehostCaseCellRow], rows):
        run_id = normalize_run_id_token(row['run_id'])
        if not run_id:
            continue
        test_name = normalize_run_test_name_token(row['test_name'])
        if not test_name:
            continue
        status = row['status']
        runresult = row['runresult']
        cpu_sec = 0.0
        runtime_sec = 0.0
        wall_sec = 0.0
        memory_kb = 0
        try:
            cpu_sec = max(0.0, float(row['cpu_sec']))
        except Exception:
            cpu_sec = 0.0
        try:
            runtime_sec = max(0.0, float(row['runtime_sec']))
        except Exception:
            runtime_sec = 0.0
        try:
            wall_sec = max(0.0, float(row['wall_sec']))
        except Exception:
            wall_sec = 0.0
        try:
            memory_kb = max(0, int(row['memory_kb']))
        except Exception:
            memory_kb = 0

        verdict = ''
        short = '..'
        metrics = 'pending'
        cpu_ms = int(round(max(cpu_sec, runtime_sec) * 1000.0))
        wall_ms = int(round(max(wall_sec, cpu_sec, runtime_sec) * 1000.0))
        reported = status == 'reported'
        if reported:
            verdict = _DOMJUDGE_VERDICT_BY_RUNRESULT.get(runresult, 'FL')
            short = run_verdict_short(verdict)
            metrics = f'{cpu_ms}ms/{run_memory_mb_text(memory_kb)}'
        elif status == 'leased':
            metrics = 'running'

        by_run = out.get(run_id)
        if by_run is None:
            by_run = {}
            out[run_id] = by_run
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
    built_columns: list[dict[str, object]],
    run_statuses: list[str],
    run_count: int,
    fallback_tests_per_solution: int,
) -> dict[str, int]:
    safe_run_count = max(0, int(run_count))
    safe_fallback_tests = max(0, int(fallback_tests_per_solution))
    case_progress_by_run = _verification_judgehost_case_progress(
        [cast(str, col.get('id')) for col in built_columns]
    )
    total_tests = 0
    completed_tests = 0
    running_tests = 0
    started_runs = 0
    for idx, col in enumerate(built_columns):
        started_runs += 1
        run_id = normalize_run_id_token(cast(str | None, col.get('id')))
        summary = cast(VerificationSummary | None, col.get('summary'))
        expected_tests = int(_verification_tests_meta_stats(summary).get("total") or 0)
        if expected_tests <= 0:
            expected_tests = safe_fallback_tests
        case_progress = case_progress_by_run.get(run_id, {})
        case_total = max(0, int(case_progress.get('total', 0)))
        case_reported = max(0, int(case_progress.get('reported', 0)))
        case_leased = max(0, int(case_progress.get('leased', 0)))
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
        run_status = run_statuses[idx] if idx < len(run_statuses) else ''
        if case_leased > 0:
            running_tests += case_leased
        elif run_status == 'running' and expected_tests > reported_tests:
            running_tests += (expected_tests - reported_tests)
    remaining_runs = max(0, safe_run_count - started_runs)
    if remaining_runs > 0 and safe_fallback_tests > 0:
        total_tests += remaining_runs * safe_fallback_tests
    return {
        'total': max(0, int(total_tests)),
        'completed': max(0, int(completed_tests)),
        'running': max(0, int(running_tests)),
    }

def _run_test_count_from_summary(summary: VerificationSummary | None) -> int:
    if summary is None:
        return 0
    if bool(summary.get('execution_skipped')):
        return 0
    tests = summary.get('tests')
    if tests is not None:
        return len(tests)
    usage = summary.get('usage')
    if usage is not None:
        try:
            return max(0, int(usage.get('tests', 0)))
        except Exception:
            return 0
    return 0


