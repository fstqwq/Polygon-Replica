import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.service.judgehost.api import Judgehost
from app.service.judgehost.ports.completion import CaseTerminalReport
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.payload import prepared_payload_for_uploaded_source


_SANITY_ACCEPTED_PROGRAM_ID = "sanity-accepted"
_SANITY_SAMPLE_OUTPUT_PROGRAM_PREFIX = "sanity-sample-output"


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


def _first_test_result(case_result: CaseTerminalReport) -> dict[str, object] | None:
    tests = case_result["summary"].get("tests")
    if not isinstance(tests, list) or not tests:
        return None
    first = tests[0]
    if not isinstance(first, dict):
        return None
    return first


def _result_verdict(case_result: CaseTerminalReport) -> tuple[str, str]:
    first = _first_test_result(case_result)
    if first is not None:
        verdict = str(first.get("verdict") or "FL").upper()
        message = str(first.get("message") or "")
        if message:
            return (verdict, message)
        return (verdict, str(case_result.get("error") or ""))
    return ("FL", str(case_result.get("error") or "judgehost sample validation failed"))


def _result_output_ref(case_result: CaseTerminalReport) -> str:
    first = _first_test_result(case_result)
    if first is None:
        return ""
    output_ref = str(first.get("output_ref") or "")
    if output_ref:
        return output_ref
    passes = first.get("passes")
    if not isinstance(passes, list) or not passes:
        return ""
    final_pass = passes[-1]
    if not isinstance(final_pass, dict):
        return ""
    return str(final_pass.get("output_ref") or "")


def _custom_input_expected_answer(
    *,
    problem: str,
    user: str,
    verification_id: str,
    plan: VerificationTestPlan,
    accepted_source_label: str,
    accepted_source_name: str,
    accepted_source_file: PayloadFile | None,
    run_verification_payload_base: dict[str, object],
    bypass_case_result_cache: bool,
    service_class: str,
    judgehost: Judgehost,
    runtime_blob_store: RuntimeBlobStore,
) -> PayloadFile:
    if not accepted_source_label or not accepted_source_name or accepted_source_file is None:
        raise RuntimeError("accepted solution source is required for custom sample input validation")
    input_file = runtime_blob_store.put_bytes(plan.sample_input_text.encode("utf-8"))
    empty_file = runtime_blob_store.put_bytes(b"")
    run_id = _validation_run_id(
        verification_id=verification_id,
        test_name=plan.test_name,
        sample_output_text=f"accepted\0{plan.sample_input_text}",
    )
    prepared = prepared_payload_for_uploaded_source(
        source_label=accepted_source_label,
        run_id=run_id,
        test_name=plan.test_name,
        input_file=input_file,
        answer_file=empty_file,
        verification_payload_base=run_verification_payload_base,
    )
    task_id = judgehost.enqueue_task(
        problem=problem,
        username=user,
        artifact_verification_id=verification_id,
        submission_path=None,
        upload_content=None,
        upload_file=accepted_source_file,
        upload_filename=accepted_source_name,
        run_id=run_id,
        selected_tests=[plan.test_name],
        verification_id=verification_id,
        verification_program_id=_SANITY_ACCEPTED_PROGRAM_ID,
        expected_behavior="accepted",
        verification_source="main-correct",
        task_kind="main-correct",
        bypass_case_result_cache=bypass_case_result_cache,
        compile_only=False,
        persist_verification_run=False,
        prepared_payload=prepared,
        service_class=service_class,
    )
    case_result = judgehost.wait_for_task_case_result(
        task_id,
        plan.test_name,
    )
    verdict, message = _result_verdict(case_result)
    if verdict != "OK":
        raise RuntimeError(message or "accepted solution failed on custom sample input")
    output_ref, _case_id = judgehost.case_output_for_task(
        task_id,
        plan.test_name,
    )
    if not output_ref:
        output_ref = _result_output_ref(case_result)
    answer_file = runtime_blob_store.descriptor(output_ref)
    if answer_file is None:
        raise RuntimeError("accepted solution output is unavailable for custom sample input")
    return answer_file


def validate_custom_sample_outputs(
    *,
    problem: str,
    user: str,
    verification_id: str,
    logs_dir: Path,
    test_plans: list[VerificationTestPlan],
    accepted_source_label: str = "",
    accepted_source_name: str = "",
    accepted_source_file: PayloadFile | None = None,
    run_verification_payload_base: dict[str, object] | None = None,
    bypass_case_result_cache: bool = False,
    service_class: str = "background",
    judgehost: Judgehost,
    runtime_blob_store: RuntimeBlobStore,
) -> SampleOutputValidationResult:
    log_path = logs_dir / "validate.log"
    lines: list[str] = []
    validated_count = 0
    candidate_plans = [
        plan
        for plan in test_plans
        if plan.sample and plan.sample_output_text and plan.sample_output_validate
    ]
    opened_program_ids: list[str] = []

    def _close_programs() -> None:
        if not opened_program_ids:
            return
        judgehost.close_programs(
            verification_id,
            list(dict.fromkeys(opened_program_ids)),
        )

    def _finish(result: SampleOutputValidationResult) -> SampleOutputValidationResult:
        _close_programs()
        return result

    for plan_index, plan in enumerate(candidate_plans):
        run_id = _validation_run_id(
            verification_id=verification_id,
            test_name=plan.test_name,
            sample_output_text=plan.sample_output_text,
        )
        validation_program_id = (
            f"{_SANITY_SAMPLE_OUTPUT_PROGRAM_PREFIX}-{plan_index}"
        )
        prepared_payload = None
        try:
            if plan.sample_input_custom:
                if run_verification_payload_base is None:
                    raise RuntimeError("verification payload is required for custom sample input validation")
                opened_program_ids.append(_SANITY_ACCEPTED_PROGRAM_ID)
                input_file = runtime_blob_store.put_bytes(
                    plan.sample_input_text.encode("utf-8")
                )
                answer_file = _custom_input_expected_answer(
                    problem=problem,
                    user=user,
                    verification_id=verification_id,
                    plan=plan,
                    accepted_source_label=accepted_source_label,
                    accepted_source_name=accepted_source_name,
                    accepted_source_file=accepted_source_file,
                    run_verification_payload_base=run_verification_payload_base,
                    bypass_case_result_cache=bypass_case_result_cache,
                    service_class=service_class,
                    judgehost=judgehost,
                    runtime_blob_store=runtime_blob_store,
                )
                prepared_payload = prepared_payload_for_uploaded_source(
                    source_label="custom_sample_output.py",
                    run_id=run_id,
                    test_name=plan.test_name,
                    input_file=input_file,
                    answer_file=answer_file,
                    verification_payload_base=run_verification_payload_base,
                )
            validation_source_file = runtime_blob_store.put_bytes(
                _validation_source_bytes(plan.sample_output_text)
            )
            task_id = judgehost.enqueue_task(
                problem=problem,
                username=user,
                artifact_verification_id=verification_id,
                submission_path=None,
                upload_content=None,
                upload_file=validation_source_file,
                upload_filename="custom_sample_output.py",
                run_id=run_id,
                selected_tests=[plan.test_name],
                verification_id=verification_id,
                verification_program_id=validation_program_id,
                expected_behavior="accepted",
                verification_source="sanity-check",
                bypass_case_result_cache=bypass_case_result_cache,
                compile_only=False,
                persist_verification_run=False,
                prepared_payload=prepared_payload,
                service_class=service_class,
            )
            opened_program_ids.append(validation_program_id)
            case_result = judgehost.wait_for_task_case_result(
                task_id,
                plan.test_name,
            )
        except Exception as exc:
            failure = str(exc) or "judgehost sample validation failed"
            error_text = (
                f"custom sample output failed on {plan.test_name}: {failure}"
            )
            lines.append(f"{plan.test_name}: failed - {failure}")
            _write_log(log_path, lines)
            return _finish(
                SampleOutputValidationResult(
                    status="failed",
                    validated_count=validated_count,
                    failed_test=plan.test_name,
                    error=error_text,
                )
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
        return _finish(
            SampleOutputValidationResult(
                status="failed",
                validated_count=validated_count,
                failed_test=plan.test_name,
                error=error_text,
            )
        )
    _write_log(log_path, lines)
    return _finish(
        SampleOutputValidationResult(
            status="passed",
            validated_count=validated_count,
            failed_test="",
            error="",
        )
    )


class VerificationSampleOutputService:
    """Validate authored sample output through the injected execution runtime."""

    def __init__(
        self,
        judgehost: Judgehost,
        runtime_blob_store: RuntimeBlobStore,
    ) -> None:
        self._judgehost = judgehost
        self._runtime_blob_store = runtime_blob_store

    def validate(
        self,
        *,
        problem: str,
        user: str,
        verification_id: str,
        logs_dir: Path,
        test_plans: list[VerificationTestPlan],
        accepted_source_label: str = "",
        accepted_source_name: str = "",
        accepted_source_file: PayloadFile | None = None,
        run_verification_payload_base: dict[str, object] | None = None,
        bypass_case_result_cache: bool = False,
        service_class: str = "background",
    ) -> SampleOutputValidationResult:
        return validate_custom_sample_outputs(
            problem=problem,
            user=user,
            verification_id=verification_id,
            logs_dir=logs_dir,
            test_plans=test_plans,
            accepted_source_label=accepted_source_label,
            accepted_source_name=accepted_source_name,
            accepted_source_file=accepted_source_file,
            run_verification_payload_base=run_verification_payload_base,
            bypass_case_result_cache=bypass_case_result_cache,
            service_class=service_class,
            judgehost=self._judgehost,
            runtime_blob_store=self._runtime_blob_store,
        )
