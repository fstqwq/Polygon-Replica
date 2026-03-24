from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.impl.runtime.config import config

from .verification_dag_plan import VerificationTestPlan


@dataclass(frozen=True)
class SampleOutputValidationResult:
    status: str
    validated_count: int
    failed_test: str
    error: str


def _write_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    log_path.write_text(payload, encoding="utf-8")


def _validation_run_id(*, verification_id: str, test_name: str, sample_output_text: str) -> str:
    digest = hashlib.sha256()
    digest.update(verification_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(test_name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(sample_output_text.encode("utf-8"))
    return f"r-sanity-{digest.hexdigest()[:16]}"


def _validation_source_bytes(sample_output_text: str) -> bytes:
    encoded = base64.b64encode(sample_output_text.encode("utf-8")).decode("ascii")
    source_text = (
        "import base64\n"
        "import sys\n"
        f"sys.stdout.buffer.write(base64.b64decode('{encoded}'))\n"
    )
    return source_text.encode("utf-8")


def _result_verdict(case_result: dict[str, object]) -> tuple[str, str]:
    summary = dict(case_result.get("summary") or {})
    tests = list(summary.get("tests") or [])
    if tests:
        first = dict(tests[0] or {})
        verdict = str(first.get("verdict") or "FL").upper()
        message = str(first.get("message") or "")
        if message:
            return (verdict, message)
        return (verdict, str(case_result.get("error") or ""))
    return ("FL", str(case_result.get("error") or "judgehost sample validation failed"))


def validate_custom_sample_outputs(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
) -> SampleOutputValidationResult:
    log_path = logs_dir / "validate.log"
    lines: list[str] = []
    validated_count = 0
    skipped_count = 0
    candidate_plans = [
        plan
        for plan in test_plans
        if plan.sample and plan.sample_output_text and plan.sample_output_validate
    ]
    for plan in candidate_plans:
        if plan.sample_input_custom and (not plan.uses_custom_sample_input):
            lines.append(f"skip {plan.test_name}: custom sample input requires sample-only verification")
            skipped_count += 1
            continue
        run_id = _validation_run_id(
            verification_id=verification_id,
            test_name=plan.test_name,
            sample_output_text=plan.sample_output_text,
        )
        try:
            task_id = config.judgehost_task_service.enqueue_task(
                problem=problem,
                username=user,
                artifact_verification_id=verification_id,
                mode=mode,
                submission_path=None,
                upload_content=_validation_source_bytes(plan.sample_output_text),
                upload_filename="custom_sample_output.py",
                run_id=run_id,
                selected_tests=[plan.test_name],
                verification_id=f"{verification_id}-sanity",
                verification_run_ids=[run_id],
                expected_behavior="accepted",
                verification_source="sanity-check",
                force_recompile=False,
                compile_only=False,
                persist_verification_run=False,
            )
            case_result = config.judgehost_task_service.wait_for_task_case_result(task_id, plan.test_name)
        except Exception as exc:
            error_text = f"custom sample output failed on {plan.test_name}: {str(exc) or 'judgehost sample validation failed'}"
            lines.append(f"{plan.test_name}: failed - {str(exc) or 'judgehost sample validation failed'}")
            _write_log(log_path, lines)
            return SampleOutputValidationResult(
                status="failed",
                validated_count=validated_count,
                failed_test=plan.test_name,
                error=error_text,
            )
        verdict, message = _result_verdict(case_result)
        if verdict == "OK":
            validated_count += 1
            lines.append(f"{plan.test_name}: ok")
            continue
        detail = message or "wrong answer"
        error_text = f"custom sample output failed on {plan.test_name}: {detail}"
        lines.append(f"{plan.test_name}: failed - {detail}")
        _write_log(log_path, lines)
        return SampleOutputValidationResult(
            status="failed",
            validated_count=validated_count,
            failed_test=plan.test_name,
            error=error_text,
        )
    _write_log(log_path, lines)
    if skipped_count > 0:
        return SampleOutputValidationResult(
            status="unknown",
            validated_count=validated_count,
            failed_test="",
            error="custom sample input validation requires sample-only verification",
        )
    return SampleOutputValidationResult(
        status="passed",
        validated_count=validated_count,
        failed_test="",
        error="",
    )
