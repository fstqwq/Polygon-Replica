from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.service.verification.types import Status

from .sample_output_validation import validate_custom_sample_outputs
from .verification_dag_plan import VerificationTestPlan

SANITY_PENDING = "pending"
SANITY_RUNNING = "running"
SANITY_PASSED = "passed"
SANITY_FAILED = "failed"
SANITY_UNKNOWN = "unknown"
SANITY_SKIPPED = "skipped"
CUSTOM_SAMPLE_OUTPUT_CHECK = "custom_sample_output"


@dataclass(frozen=True)
class VerificationSanityResult:
    status: str
    check_name: str
    checked_count: int
    failed_test: str
    error: str


def planned_sanity_checks(test_plans: list[VerificationTestPlan]) -> list[str]:
    for plan in test_plans:
        if plan.sample and plan.sample_output_text and plan.sample_output_validate:
            return [CUSTOM_SAMPLE_OUTPUT_CHECK]
    return []


def effective_verification_status(
    *,
    task_status: str,
    counts: dict[str, object],
    sanity_checks: list[str],
    sanity_status: str,
) -> tuple[str, bool]:
    has_pending_or_running = bool(int(counts["pending"]) or int(counts["queued"]) or int(counts["running"]))
    if has_pending_or_running:
        return (task_status, False)
    if task_status != Status.OK.value:
        return (task_status, task_status in {Status.OK.value, Status.FAILED.value})
    if not sanity_checks:
        return (Status.OK.value, True)
    if sanity_status in {SANITY_PENDING, SANITY_RUNNING}:
        return (Status.RUNNING.value, False)
    if sanity_status == SANITY_FAILED:
        return (Status.FAILED.value, True)
    return (Status.OK.value, True)


def run_verification_sanity_checks(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
) -> VerificationSanityResult:
    checks = planned_sanity_checks(test_plans)
    if not checks:
        return VerificationSanityResult(
            status=SANITY_PASSED,
            check_name="",
            checked_count=0,
            failed_test="",
            error="",
        )
    result = validate_custom_sample_outputs(
        problem=problem,
        user=user,
        verification_id=verification_id,
        mode=mode,
        logs_dir=logs_dir,
        test_plans=test_plans,
    )
    return VerificationSanityResult(
        status=result.status,
        check_name=CUSTOM_SAMPLE_OUTPUT_CHECK,
        checked_count=int(result.validated_count),
        failed_test=result.failed_test,
        error=result.error,
    )
