from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.impl.runtime.config import config
from app.service.verification.types import VerificationStatus

from app.impl.workspace.boundary_coverage import (
    BOUNDARY_COVERAGE_CHECK,
    boundary_coverage_missing_message,
    boundary_coverage_from_feedback,
)
from app.impl.workspace.sample_output_validation import _result_verdict, validate_custom_sample_outputs
from app.impl.workspace.runtime_threshold import (
    SUMMARY_RUNTIME_THRESHOLD_CHECK,
    evaluate_summary_runtime_threshold,
    runtime_threshold_reason,
)
from app.service.verification.plan import VerificationTestPlan
from app.service.platform.runtime_blob_store import PayloadFile

SANITY_PENDING = "pending"
SANITY_RUNNING = "running"
SANITY_PASSED = "passed"
SANITY_FAILED = "failed"
SANITY_SKIPPED = "skipped"
SANITY_WARNING = "warning"
CUSTOM_SAMPLE_OUTPUT_CHECK = "custom_sample_output"
EMPTY_OUTPUT_STABILITY_CHECK = "empty_output_stability"
UNICODE_OUTPUT_STABILITY_CHECK = "unicode_output_stability"


@dataclass(frozen=True)
class VerificationSanityMessage:
    severity: str
    test_name: str
    message: str


@dataclass(frozen=True)
class VerificationSanityCheckResult:
    name: str
    status: str
    checked_count: int
    messages: tuple[VerificationSanityMessage, ...] = ()


@dataclass(frozen=True)
class VerificationSanityResult:
    status: str
    check_name: str
    checked_count: int
    failed_test: str
    error: str
    check_results: tuple[VerificationSanityCheckResult, ...] = ()


@dataclass(frozen=True)
class _StabilityProbe:
    check_name: str
    upload_filename: str
    source_bytes: bytes


def planned_sanity_checks(test_plans: list[VerificationTestPlan]) -> list[str]:
    checks: list[str] = []
    if test_plans:
        checks.extend(
            [
                EMPTY_OUTPUT_STABILITY_CHECK,
                UNICODE_OUTPUT_STABILITY_CHECK,
                SUMMARY_RUNTIME_THRESHOLD_CHECK,
                BOUNDARY_COVERAGE_CHECK,
            ]
        )
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
    if task_status != VerificationStatus.OK.value:
        return (
            task_status,
            task_status
            in {
                VerificationStatus.OK.value,
                VerificationStatus.FAILED.value,
                VerificationStatus.CANCELLED.value,
            },
        )
    if not sanity_checks:
        return (VerificationStatus.OK.value, True)
    if sanity_status in {SANITY_PENDING, SANITY_RUNNING}:
        return (VerificationStatus.RUNNING.value, False)
    if sanity_status in {SANITY_FAILED, SANITY_WARNING}:
        return (VerificationStatus.OK.value, True)
    return (VerificationStatus.OK.value, True)


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
    bypass_case_result_cache: bool,
) -> tuple[str, str]:
    run_id = _stability_run_id(
        verification_id=verification_id,
        test_name=plan.test_name,
        check_name=probe.check_name,
    )
    program_id = f"sanity-{probe.check_name}"
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
        verification_id=verification_id,
        verification_program_id=program_id,
        expected_behavior="unknown",
        verification_source="sanity-check",
        bypass_case_result_cache=bypass_case_result_cache,
        compile_only=False,
        persist_verification_run=False,
    )
    try:
        return _result_verdict(config.judgehost_task_service.wait_for_task_case_result(task_id, plan.test_name))
    finally:
        config.judgehost_task_service.close_programs(
            verification_id,
            [program_id],
        )


def _run_stability_checks(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
    bypass_case_result_cache: bool,
) -> list[VerificationSanityCheckResult]:
    probe_plan = next((plan for plan in test_plans if plan.test_name), None)
    if probe_plan is None:
        return [
            VerificationSanityCheckResult(name=probe.check_name, status=SANITY_PASSED, checked_count=0)
            for probe in _stability_probes()
        ]
    log_path = logs_dir / "stability.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    results: list[VerificationSanityCheckResult] = []
    for probe in _stability_probes():
        try:
            verdict, message = _run_stability_probe(
                problem=problem,
                user=user,
                verification_id=verification_id,
                mode=mode,
                plan=probe_plan,
                probe=probe,
                bypass_case_result_cache=bypass_case_result_cache,
            )
        except Exception as exc:
            detail = str(exc) or "judgehost stability probe failed"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - {detail}")
            results.append(
                VerificationSanityCheckResult(
                    name=probe.check_name,
                    status=SANITY_FAILED,
                    checked_count=0,
                    messages=(
                        VerificationSanityMessage(
                            severity=SANITY_FAILED,
                            test_name=probe_plan.test_name,
                            message=f"{probe.check_name} failed on {probe_plan.test_name}: {detail}",
                        ),
                    ),
                )
            )
            continue
        if verdict in {"OK", "AC"}:
            detail = message or "probe was accepted"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - accepted ({detail})")
            results.append(
                VerificationSanityCheckResult(
                    name=probe.check_name,
                    status=SANITY_FAILED,
                    checked_count=0,
                    messages=(
                        VerificationSanityMessage(
                            severity=SANITY_FAILED,
                            test_name=probe_plan.test_name,
                            message=f"{probe.check_name} failed on {probe_plan.test_name}: got {verdict}; {detail}",
                        ),
                    ),
                )
            )
            continue
        if verdict == "FL":
            detail = message or "probe caused FL"
            lines.append(f"{probe.check_name} {probe_plan.test_name}: failed - FL ({detail})")
            results.append(
                VerificationSanityCheckResult(
                    name=probe.check_name,
                    status=SANITY_FAILED,
                    checked_count=0,
                    messages=(
                        VerificationSanityMessage(
                            severity=SANITY_FAILED,
                            test_name=probe_plan.test_name,
                            message=f"{probe.check_name} failed on {probe_plan.test_name}: got FL; {detail}",
                        ),
                    ),
                )
            )
            continue
        lines.append(f"{probe.check_name} {probe_plan.test_name}: ok - {verdict or 'non-AC'}")
        results.append(VerificationSanityCheckResult(name=probe.check_name, status=SANITY_PASSED, checked_count=1))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def _aggregate_sanity_results(check_results: list[VerificationSanityCheckResult]) -> VerificationSanityResult:
    checked_count = sum(int(item.checked_count) for item in check_results)
    selected: VerificationSanityCheckResult | None = None
    for status in (SANITY_FAILED, SANITY_WARNING):
        selected = next((item for item in check_results if item.status == status), None)
        if selected is not None:
            break
    if selected is None:
        return VerificationSanityResult(
            status=SANITY_PASSED,
            check_name="",
            checked_count=checked_count,
            failed_test="",
            error="",
            check_results=tuple(check_results),
        )
    message = selected.messages[0] if selected.messages else None
    return VerificationSanityResult(
        status=selected.status,
        check_name=selected.name,
        checked_count=checked_count,
        failed_test=message.test_name if message is not None else "",
        error=message.message if message is not None else "",
        check_results=tuple(check_results),
    )


def run_verification_sanity_checks(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
    accepted_source_label: str = "",
    accepted_source_name: str = "",
    accepted_source_file: PayloadFile | None = None,
    run_verification_payload_base: dict[str, object] | None = None,
    generate_feedback_by_test: dict[str, str] | None = None,
    runtime_columns: list[dict[str, object]] | None = None,
    time_limit_ms: int = 0,
    bypass_case_result_cache: bool = False,
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
    check_results = _run_stability_checks(
        problem=problem,
        user=user,
        verification_id=verification_id,
        mode=mode,
        logs_dir=logs_dir,
        test_plans=test_plans,
        bypass_case_result_cache=bypass_case_result_cache,
    )
    if SUMMARY_RUNTIME_THRESHOLD_CHECK in checks:
        runtime_checked_count = 0
        runtime_messages: list[VerificationSanityMessage] = []
        runtime_log = logs_dir / "summary-runtime-threshold.log"
        runtime_log.parent.mkdir(parents=True, exist_ok=True)
        for column in list(runtime_columns or []):
            summary = dict(column.get("summary") or {})
            source = str(column.get("source") or summary.get("source") or "")
            report = evaluate_summary_runtime_threshold(
                summary=summary,
                source=source,
                time_limit_ms=int(time_limit_ms),
            )
            runtime_checked_count += int(report.checked_count)
            if report.warning_hit is not None:
                reason = runtime_threshold_reason(report.warning_hit, summary_has_tl=bool(column.get("summary_has_tl")))
                runtime_messages.append(VerificationSanityMessage(severity=SANITY_WARNING, test_name="", message=reason))
        if runtime_messages:
            runtime_log.write_text("\n".join(item.message for item in runtime_messages) + "\n", encoding="utf-8")
            runtime_status = SANITY_WARNING
        else:
            runtime_log.write_text("summary runtime threshold ok\n", encoding="utf-8")
            runtime_status = SANITY_PASSED
        check_results.append(
            VerificationSanityCheckResult(
                name=SUMMARY_RUNTIME_THRESHOLD_CHECK,
                status=runtime_status,
                checked_count=runtime_checked_count,
                messages=tuple(runtime_messages),
            )
        )
    boundary_result = boundary_coverage_from_feedback(
        feedback_by_test=dict(generate_feedback_by_test or {}),
        test_plans=test_plans,
    )
    boundary_log = logs_dir / "boundary.log"
    boundary_log.parent.mkdir(parents=True, exist_ok=True)
    boundary_messages = tuple(
        VerificationSanityMessage(
            severity=SANITY_WARNING,
            test_name="",
            message=boundary_coverage_missing_message(item),
        )
        for item in boundary_result.missing
    )
    if boundary_result.status == SANITY_WARNING:
        boundary_log.write_text(
            "\n".join(item.message for item in boundary_messages) + "\n",
            encoding="utf-8",
        )
    else:
        boundary_log.write_text("boundary coverage ok\n", encoding="utf-8")
    check_results.append(
        VerificationSanityCheckResult(
            name=BOUNDARY_COVERAGE_CHECK,
            status=boundary_result.status,
            checked_count=int(boundary_result.checked_count),
            messages=boundary_messages,
        )
    )
    if CUSTOM_SAMPLE_OUTPUT_CHECK in checks:
        result = validate_custom_sample_outputs(
            problem=problem,
            user=user,
            verification_id=verification_id,
            mode=mode,
            logs_dir=logs_dir,
            test_plans=test_plans,
            accepted_source_label=accepted_source_label,
            accepted_source_name=accepted_source_name,
            accepted_source_file=accepted_source_file,
            run_verification_payload_base=run_verification_payload_base,
            bypass_case_result_cache=bypass_case_result_cache,
        )
        messages: tuple[VerificationSanityMessage, ...] = ()
        if result.status == SANITY_FAILED:
            messages = (
                VerificationSanityMessage(
                    severity=SANITY_FAILED,
                    test_name=result.failed_test,
                    message=result.error,
                ),
            )
        check_results.append(
            VerificationSanityCheckResult(
                name=CUSTOM_SAMPLE_OUTPUT_CHECK,
                status=result.status,
                checked_count=int(result.validated_count),
                messages=messages,
            )
        )
    return _aggregate_sanity_results(check_results)
