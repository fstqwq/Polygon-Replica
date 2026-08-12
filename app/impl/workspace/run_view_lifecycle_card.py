from __future__ import annotations

from typing import TypedDict

from app.impl.runtime.dependency import runtime

from app.impl.workspace.context_operation import dedupe_preserve_order
from app.impl.workspace.context_run_detail import normalize_run_id_token, normalize_run_test_name_token
VerificationDetailSummaryRow = TypedDict(
    "VerificationDetailSummaryRow",
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

def load_verification_detail_summary(problem_id: int, verification_id: str) -> VerificationDetailSummaryRow | dict[str, object]:
    safe_verification_id = normalize_run_id_token(verification_id)
    if not safe_verification_id:
        return {}
    verification_row = runtime().verification_service.verification_record(safe_verification_id)
    if verification_row is None or int(verification_row['problem_id']) != int(problem_id):
        return {}
    detail = runtime().verification_service.verification_detail(safe_verification_id)
    details = {
        **detail,
        **runtime().verification_service.verification_runtime_summary(safe_verification_id),
        'verification_id': safe_verification_id,
        'finished_at': verification_row['finished_at'],
    }
    details['status'] = verification_row['status']
    details['artifact_verification_id'] = safe_verification_id
    return {
        'details': details,
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
