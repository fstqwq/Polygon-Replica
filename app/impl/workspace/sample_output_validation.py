from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.impl.runtime.config import config

from app.service.verification.plan import VerificationTestPlan
from .verification_payload import prepared_payload_for_uploaded_source


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


def _result_output_ref(case_result: dict[str, object]) -> str:
    summary = dict(case_result.get("summary") or {})
    tests = list(summary.get("tests") or [])
    if not tests:
        return ""
    first = dict(tests[0] or {})
    output_ref = str(first.get("output_ref") or "")
    if output_ref:
        return output_ref
    passes = list(first.get("passes") or [])
    if not passes:
        return ""
    return str(dict(passes[-1] or {}).get("output_ref") or "")


def _custom_input_expected_answer(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    plan: VerificationTestPlan,
    accepted_source_label: str,
    accepted_source_name: str,
    accepted_source_bytes: bytes,
    run_verification_payload_base: dict[str, object],
) -> bytes:
    if not accepted_source_label or not accepted_source_name or not accepted_source_bytes:
        raise RuntimeError("accepted solution source is required for custom sample input validation")
    input_bytes = plan.sample_input_text.encode("utf-8")
    run_id = _validation_run_id(
        verification_id=verification_id,
        test_name=plan.test_name,
        sample_output_text=f"accepted\0{plan.sample_input_text}",
    )
    prepared = prepared_payload_for_uploaded_source(
        source_label=accepted_source_label,
        run_id=run_id,
        test_name=plan.test_name,
        input_bytes=input_bytes,
        answer_bytes=b"",
        verification_payload_base=run_verification_payload_base,
    )
    task_id = config.judgehost_task_service.enqueue_task(
        problem=problem,
        username=user,
        artifact_verification_id=verification_id,
        mode=mode,
        submission_path=None,
        upload_content=accepted_source_bytes,
        upload_filename=accepted_source_name,
        run_id=run_id,
        selected_tests=[plan.test_name],
        verification_id=f"{verification_id}-sanity",
        verification_run_ids=[run_id],
        expected_behavior="accepted",
        verification_source="main-correct",
        task_kind="main-correct",
        force_recompile=False,
        compile_only=False,
        persist_verification_run=False,
        prepared_payload=prepared,
    )
    case_result = config.judgehost_task_service.wait_for_task_case_result(task_id, plan.test_name)
    verdict, message = _result_verdict(case_result)
    if verdict != "OK":
        raise RuntimeError(message or "accepted solution failed on custom sample input")
    output_ref, work_root, _case_id = config.judgehost_task_service.domjudge_case_output_for_task(
        task_id,
        plan.test_name,
    )
    if not output_ref:
        output_ref = _result_output_ref(case_result)
        work_root = None
    answer_bytes = config.judgehost_task_service.resolve_artifact_blob(output_ref, work_root=work_root)
    if answer_bytes is None:
        raise RuntimeError("accepted solution output is unavailable for custom sample input")
    return answer_bytes


def validate_custom_sample_outputs(
    *,
    problem: str,
    user: str,
    verification_id: str,
    mode: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
    accepted_source_label: str = "",
    accepted_source_name: str = "",
    accepted_source_bytes: bytes = b"",
    run_verification_payload_base: dict[str, object] | None = None,
) -> SampleOutputValidationResult:
    log_path = logs_dir / "validate.log"
    lines: list[str] = []
    validated_count = 0
    candidate_plans = [
        plan
        for plan in test_plans
        if plan.sample and plan.sample_output_text and plan.sample_output_validate
    ]
    for plan in candidate_plans:
        run_id = _validation_run_id(
            verification_id=verification_id,
            test_name=plan.test_name,
            sample_output_text=plan.sample_output_text,
        )
        prepared_payload = None
        try:
            if plan.sample_input_custom:
                if run_verification_payload_base is None:
                    raise RuntimeError("verification payload is required for custom sample input validation")
                input_bytes = plan.sample_input_text.encode("utf-8")
                answer_bytes = _custom_input_expected_answer(
                    problem=problem,
                    user=user,
                    verification_id=verification_id,
                    mode=mode,
                    plan=plan,
                    accepted_source_label=accepted_source_label,
                    accepted_source_name=accepted_source_name,
                    accepted_source_bytes=accepted_source_bytes,
                    run_verification_payload_base=run_verification_payload_base,
                )
                prepared_payload = prepared_payload_for_uploaded_source(
                    source_label="custom_sample_output.py",
                    run_id=run_id,
                    test_name=plan.test_name,
                    input_bytes=input_bytes,
                    answer_bytes=answer_bytes,
                    verification_payload_base=run_verification_payload_base,
                )
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
                prepared_payload=prepared_payload,
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
    return SampleOutputValidationResult(
        status="passed",
        validated_count=validated_count,
        failed_test="",
        error="",
    )
