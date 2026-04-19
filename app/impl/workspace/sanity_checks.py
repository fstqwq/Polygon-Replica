from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.impl.runtime.config import config
from app.service.verification.types import Status

from .sample_output_validation import _result_verdict, validate_custom_sample_outputs
from .verification_dag_plan import VerificationTestPlan

SANITY_PENDING = "pending"
SANITY_RUNNING = "running"
SANITY_PASSED = "passed"
SANITY_FAILED = "failed"
SANITY_SKIPPED = "skipped"
CUSTOM_SAMPLE_OUTPUT_CHECK = "custom_sample_output"
EMPTY_OUTPUT_STABILITY_CHECK = "empty_output_stability"
UNICODE_OUTPUT_STABILITY_CHECK = "unicode_output_stability"


@dataclass(frozen=True)
class VerificationSanityResult:
    status: str
    check_name: str
    checked_count: int
    failed_test: str
    error: str


@dataclass(frozen=True)
class _StabilityProbe:
    check_name: str
    upload_filename: str
    source_bytes: bytes


def planned_sanity_checks(test_plans: list[VerificationTestPlan]) -> list[str]:
    checks: list[str] = []
    if test_plans:
        checks.extend([EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK])
    for plan in test_plans:
        if plan.sample and plan.sample_output_text and plan.sample_output_validate:
            checks.append(CUSTOM_SAMPLE_OUTPUT_CHECK)
            break
    return checks


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


def _stability_run_id(*, verification_id: str, test_name: str, check_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(verification_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(test_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(check_name.encode("utf-8"))
    return f"r-sanity-{digest.hexdigest()[:16]}"


def _output_probe_source(output_text: str) -> bytes:
    if not output_text:
        return b"import sys\n"
    encoded = base64.b64encode(output_text.encode("utf-8")).decode("ascii")
    source_text = (
        "import base64\n"
        "import sys\n"
        f"sys.stdout.buffer.write(base64.b64decode('{encoded}'))\n"
    )
    return source_text.encode("utf-8")


def _stability_probes() -> list[_StabilityProbe]:
    return [
        _StabilityProbe(
            check_name=EMPTY_OUTPUT_STABILITY_CHECK,
            upload_filename="sanity_empty_output.py",
            source_bytes=_output_probe_source(""),
        ),
        _StabilityProbe(
            check_name=UNICODE_OUTPUT_STABILITY_CHECK,
            upload_filename="sanity_unicode_output.py",
            source_bytes=_output_probe_source("\u4f60\u597d\U0001f642\n"),
        ),
    ]


def _run_stability_probe(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    plan: VerificationTestPlan,
    probe: _StabilityProbe,
) -> tuple[str, str]:
    run_id = _stability_run_id(
        verification_id=verification_id,
        test_name=plan.test_name,
        check_name=probe.check_name,
    )
    task_id = config.judgehost_task_service.enqueue_task(
        problem=problem,
        username=user,
        artifact_verification_id=verification_id,
        mode=mode,
        submission_path=None,
        upload_content=probe.source_bytes,
        upload_filename=probe.upload_filename,
        run_id=run_id,
        selected_tests=[plan.test_name],
        verification_id=f"{verification_id}-sanity",
        verification_run_ids=[run_id],
        expected_behavior="unknown",
        verification_source="sanity-check",
        force_recompile=False,
        compile_only=False,
        persist_verification_run=False,
    )
    return _result_verdict(config.judgehost_task_service.wait_for_task_case_result(task_id, plan.test_name))


def _run_stability_checks(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
) -> VerificationSanityResult:
    probe_plan = next((plan for plan in test_plans if plan.test_name), None)
    if probe_plan is None:
        return VerificationSanityResult(
            status=SANITY_PASSED,
            check_name="",
            checked_count=0,
            failed_test="",
            error="",
        )
    log_path = logs_dir / "stability.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    checked_count = 0
    for probe in _stability_probes():
        try:
            verdict, message = _run_stability_probe(
                problem=problem,
                user=user,
                verification_id=verification_id,
                mode=mode,
                plan=probe_plan,
                probe=probe,
            )
        except Exception as exc:
            detail = str(exc) or "judgehost stability probe failed"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - {detail}")
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return VerificationSanityResult(
                status=SANITY_FAILED,
                check_name=probe.check_name,
                checked_count=checked_count,
                failed_test=probe_plan.test_name,
                error=f"{probe.check_name} failed on {probe_plan.test_name}: {detail}",
            )
        if verdict in {"OK", "AC"}:
            detail = message or "probe was accepted"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - accepted ({detail})")
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return VerificationSanityResult(
                status=SANITY_FAILED,
                check_name=probe.check_name,
                checked_count=checked_count,
                failed_test=probe_plan.test_name,
                error=f"{probe.check_name} failed on {probe_plan.test_name}: got {verdict}; {detail}",
            )
        if verdict == "FL":
            detail = message or "probe caused FL"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - FL ({detail})")
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return VerificationSanityResult(
                status=SANITY_FAILED,
                check_name=probe.check_name,
                checked_count=checked_count,
                failed_test=probe_plan.test_name,
                error=f"{probe.check_name} failed on {probe_plan.test_name}: got FL; {detail}",
            )
        checked_count += 1
        lines.append(f"{probe.check_name} {probe_plan.test_name}: ok - {verdict or 'non-AC'}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return VerificationSanityResult(
        status=SANITY_PASSED,
        check_name="",
        checked_count=checked_count,
        failed_test="",
        error="",
    )


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
    stability_result = _run_stability_checks(
        problem=problem,
        user=user,
        verification_id=verification_id,
        mode=mode,
        logs_dir=logs_dir,
        test_plans=test_plans,
    )
    if stability_result.status == SANITY_FAILED:
        return stability_result
    checked_count = int(stability_result.checked_count)
    if CUSTOM_SAMPLE_OUTPUT_CHECK not in checks:
        return stability_result
    result = validate_custom_sample_outputs(
        problem=problem,
        user=user,
        verification_id=verification_id,
        mode=mode,
        logs_dir=logs_dir,
        test_plans=test_plans,
    )
    checked_count += int(result.validated_count)
    return VerificationSanityResult(
        status=result.status,
        check_name=CUSTOM_SAMPLE_OUTPUT_CHECK,
        checked_count=checked_count,
        failed_test=result.failed_test,
        error=result.error,
    )
