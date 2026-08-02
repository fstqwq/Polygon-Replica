from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.service.judgehost.limits import STORED_LOG_TRUNCATED_MARKER
from app.service.platform.error_text import (
    aux_display_text_limit_bytes,
    bounded_display_text,
    normalize_display_text,
)
from app.service.verification.task_metadata import canonical_diagnostics, diagnostics_json_text
from app.service.verification.task_scheduler import TaskExecutionResult
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.test_rows import build_verification_test_row
from app.service.verification.types import Status

TASK_GENERATE_INPUT = "generate-input"
TASK_MAIN_CORRECT = "main-correct"

_COMPILE_DIAGNOSTICS_LIMIT = 64
_ACCEPTING_VERDICTS = frozenset({"OK", "AC"})
_GENERATED_INPUT_TRUNCATION_MARKERS = (
    b"[output storage truncated after",
    STORED_LOG_TRUNCATED_MARKER,
)


@dataclass(frozen=True)
class _TaskSummaryParts:
    verdict: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    compile_log: str
    diagnostics_json: str
    error_text: str
    feedback_text: str
    output_ref: str
    answer_correct: bool


def _normalized_error_text(value: str) -> str:
    return normalize_display_text(str(value or ""))


def verification_task_fail_reason(
    task_row: VerificationTaskRow,
    *,
    error_text: str,
    fallback: str = "",
) -> str:
    detail_text = _normalized_error_text(str(error_text or fallback))
    origin_tokens = [
        str(task_row["task_kind"] or "").strip(),
        str(task_row["source_path"] or "").strip(),
        str(task_row["test_name"] or "").strip(),
    ]
    origin_text = " / ".join(token for token in origin_tokens if token)
    if origin_text and detail_text:
        return bounded_display_text(f"{origin_text}: {detail_text}")
    if origin_text:
        return bounded_display_text(origin_text)
    return bounded_display_text(detail_text)


def _answer_name(test_name: str) -> str:
    stem = Path(test_name).stem
    if not stem:
        raise RuntimeError("test name is required")
    return f"{stem}.ans"


def _materialized_output_name(test_name: str) -> str:
    stem = Path(test_name).stem
    if stem:
        return f"{stem}.out"
    return "program.out"


def _persist_verification_artifact(
    *,
    verification_id: str,
    test_name: str,
    ref_key: str,
    file_name: str,
    payload: bytes,
) -> str:
    from app.impl.runtime.config import config

    role = ref_key.removesuffix("_ref") or ref_key
    ref = config.verification_service.store_verification_blob(
        verification_id=verification_id,
        test_name=test_name,
        role=role,
        file_name=file_name,
        payload=payload,
    )
    if ref_key in {"input_ref", "answer_ref"}:
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            test_name,
            {ref_key: ref},
        )
    return ref


def _materialize_run_output(
    *,
    verification_id: str,
    artifact_run_id: str,
    judgehost_task_id: str,
    test_name: str,
    output_ref: str,
) -> tuple[str, bytes | None]:
    if (not artifact_run_id) or (not judgehost_task_id) or (not test_name):
        return ("", None)
    from app.impl.runtime.config import config

    case_output_ref, work_root, _case_id = config.judgehost_task_service.domjudge_case_output_for_task(
        judgehost_task_id,
        test_name,
    )
    resolved_output_ref = output_ref or case_output_ref
    if not resolved_output_ref:
        return ("", None)
    output_blob = config.judgehost_task_service.resolve_artifact_blob(
        resolved_output_ref,
        work_root=work_root,
    )
    if output_blob is None:
        return ("", None)
    if resolved_output_ref.startswith("cache://"):
        return (resolved_output_ref, output_blob)
    output_ref_token = _persist_verification_artifact(
        verification_id=verification_id,
        test_name=test_name,
        ref_key="output_ref",
        file_name=_materialized_output_name(test_name),
        payload=output_blob,
    )
    return (output_ref_token, output_blob)


def _verdict_from_summary(summary: dict[str, object], run_status: str) -> str:
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    if tests:
        verdict = str(tests[-1].get("verdict") or "").upper()
        if verdict:
            return verdict
    diagnostics = cast(list[dict[str, object]], summary.get("compile_diagnostics") or [])
    if diagnostics:
        return "CE"
    return "AC" if run_status == Status.OK.value else "FL"


def _summary_parts(summary: dict[str, object], *, run_status: str, error_text: str) -> _TaskSummaryParts:
    verdict = _verdict_from_summary(summary, run_status)
    aux_limit = aux_display_text_limit_bytes()
    diagnostics_meta = canonical_diagnostics(
        cast(list[dict[str, object]] | list[object] | None, summary.get("compile_diagnostics") or []),
        list_limit=_COMPILE_DIAGNOSTICS_LIMIT,
        message_limit=aux_limit,
    )
    diagnostics_json = diagnostics_json_text(diagnostics_meta["rows"])
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    feedback_source = ""
    output_ref = ""
    answer_correct = False
    runtime_sec: float | None = None
    cpu_sec: float | None = None
    wall_sec: float | None = None
    memory_kb: int | None = None
    if tests:
        test_row = dict(tests[-1])
        feedback_source = str(test_row.get("message") or "")
        output_ref = str(test_row.get("output_ref") or "")
        answer_correct = bool(test_row.get("answer_correct"))
        cpu_ms = int(test_row.get("time_user_ms") or test_row.get("time_ms") or 0)
        wall_ms = int(test_row.get("time_wall_ms") or cpu_ms)
        runtime_ms = int(test_row.get("time_ms") or cpu_ms)
        memory_kb = int(test_row.get("memory_kb") or 0)
        runtime_sec = float(runtime_ms) / 1000.0
        cpu_sec = float(cpu_ms) / 1000.0
        wall_sec = float(wall_ms) / 1000.0
    final_error_text = _normalized_error_text(str(error_text or summary.get("error") or ""))
    feedback_text = _normalized_error_text(feedback_source)
    if verdict == "CE" and (not final_error_text) and diagnostics_json == "[]":
        final_error_text = feedback_text or "compile error"
    return _TaskSummaryParts(
        verdict=verdict,
        runtime_sec=runtime_sec,
        cpu_sec=cpu_sec,
        wall_sec=wall_sec,
        memory_kb=memory_kb,
        compile_log=_normalized_error_text(str(summary.get("error") or error_text or "")),
        diagnostics_json=diagnostics_json,
        error_text=final_error_text,
        feedback_text=feedback_text,
        output_ref=output_ref,
        answer_correct=answer_correct,
    )


def _accepted_verdict(verdict: str) -> bool:
    return str(verdict or "").upper() in _ACCEPTING_VERDICTS


def _generated_input_truncated(blob: bytes) -> bool:
    return any(marker in blob for marker in _GENERATED_INPUT_TRUNCATION_MARKERS)


def _final_error_text(parts: _TaskSummaryParts, *, fallback: str) -> str:
    error_text = str(parts.error_text or "").strip()
    if error_text:
        return error_text
    feedback_text = str(parts.feedback_text or "").strip()
    if feedback_text:
        return _normalized_error_text(feedback_text)
    return _normalized_error_text(fallback)


def _task_row_to_test_row(row: VerificationTaskRow) -> dict[str, object]:
    runtime_ms = 0 if row["runtime_sec"] is None else max(0, int(round(float(row["runtime_sec"]) * 1000.0)))
    cpu_ms = runtime_ms if row["cpu_sec"] is None else max(0, int(round(float(row["cpu_sec"]) * 1000.0)))
    wall_ms = cpu_ms if row["wall_sec"] is None else max(0, int(round(float(row["wall_sec"]) * 1000.0)))
    memory_kb = 0 if row["memory_kb"] is None else max(0, int(row["memory_kb"]))
    feedback_text = str(row["feedback_text"] or row["error_text"] or "")
    return build_verification_test_row(
        test_name=str(row["test_name"]),
        verdict=str(row["verdict"] or "--"),
        time_ms=runtime_ms,
        time_user_ms=cpu_ms,
        time_wall_ms=wall_ms,
        memory_kb=memory_kb,
        message=feedback_text,
        output_ref=str(row["output_ref"] or ""),
        feedback_files=[],
        answer_correct=bool(row.get("answer_correct")),
    )


def finalize_verification_task_result(task_row: VerificationTaskRow, *, result: dict[str, object]) -> TaskExecutionResult:
    verification_id = str(task_row["verification_id"] or "")
    task_id = str(task_row["id"] or "")
    task_kind = str(task_row["task_kind"] or "")
    test_name = str(task_row["test_name"] or "")
    judgehost_task_id = str(task_row["judgehost_task_id"] or "")
    run_id = str(result.get("run_id") or str(task_row["run_id"] or ""))
    artifact_run_id = str(task_row["logical_run_id"] or run_id)
    result_summary = dict(result.get("summary") or {})
    result_status = str(result.get("status") or "")
    summary_error_text = str(result_summary.get("error") or "")
    result_error_text = str(result.get("error") or "")
    error_text = summary_error_text or result_error_text
    missing_case_result = bool(result.get("missing_case_result"))
    parts = _summary_parts(result_summary, run_status=result_status, error_text=error_text)
    cache_ref_only = (
        task_kind not in {TASK_GENERATE_INPUT, TASK_MAIN_CORRECT}
        and parts.output_ref.startswith("cache://")
    )
    if cache_ref_only:
        materialized_output_ref, materialized_output_blob = (parts.output_ref, None)
    else:
        materialized_output_ref, materialized_output_blob = _materialize_run_output(
            verification_id=verification_id,
            artifact_run_id=artifact_run_id,
            judgehost_task_id=judgehost_task_id,
            test_name=test_name,
            output_ref=parts.output_ref,
        )

    if missing_case_result:
        fail_reason = error_text or f"judgehost case result missing for {test_name}"
        return TaskExecutionResult(
            task_id=task_id,
            status=VerificationTaskStore.TASK_FAILED,
            verdict="",
            run_id=run_id,
            judgehost_task_id=judgehost_task_id,
            runtime_sec=None,
            cpu_sec=None,
            wall_sec=None,
            memory_kb=None,
            compile_log=parts.compile_log,
            diagnostics_json=parts.diagnostics_json,
            error_text=parts.error_text,
            feedback_text="",
            output_ref="",
            fail_flag_reason=(
                verification_task_fail_reason(task_row, error_text=fail_reason)
                if task_kind == TASK_MAIN_CORRECT
                else ""
            ),
            answer_correct=False,
        )

    if task_kind == TASK_GENERATE_INPUT:
        generate_input_ok = result_status == Status.OK.value and _accepted_verdict(parts.verdict)
        if not generate_input_ok:
            fail_reason = _final_error_text(
                parts,
                fallback=error_text or f"validator rejected generated input for {test_name}",
            )
            return TaskExecutionResult(
                task_id=task_id,
                status=VerificationTaskStore.TASK_FAILED,
                verdict=parts.verdict or "FL",
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                runtime_sec=parts.runtime_sec,
                cpu_sec=parts.cpu_sec,
                wall_sec=parts.wall_sec,
                memory_kb=parts.memory_kb,
                compile_log=parts.compile_log,
                diagnostics_json=parts.diagnostics_json,
                error_text=fail_reason,
                feedback_text=parts.feedback_text,
                output_ref=materialized_output_ref,
                fail_flag_reason=verification_task_fail_reason(task_row, error_text=fail_reason),
                answer_correct=parts.answer_correct,
            )
        output_blob = materialized_output_blob
        if output_blob is None:
            fail_message = f"generated input output missing for {test_name}"
            return TaskExecutionResult(
                task_id=task_id,
                status=VerificationTaskStore.TASK_FAILED,
                verdict="FL",
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                runtime_sec=parts.runtime_sec,
                cpu_sec=parts.cpu_sec,
                wall_sec=parts.wall_sec,
                memory_kb=parts.memory_kb,
                compile_log=parts.compile_log,
                diagnostics_json=parts.diagnostics_json,
                error_text=fail_message,
                feedback_text=parts.feedback_text,
                output_ref=materialized_output_ref,
                fail_flag_reason=verification_task_fail_reason(task_row, error_text=fail_message),
                answer_correct=parts.answer_correct,
            )
        if _generated_input_truncated(output_blob):
            fail_message = f"generated input output was truncated for {test_name}"
            return TaskExecutionResult(
                task_id=task_id,
                status=VerificationTaskStore.TASK_FAILED,
                verdict="FL",
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                runtime_sec=parts.runtime_sec,
                cpu_sec=parts.cpu_sec,
                wall_sec=parts.wall_sec,
                memory_kb=parts.memory_kb,
                compile_log=parts.compile_log,
                diagnostics_json=parts.diagnostics_json,
                error_text=fail_message,
                feedback_text=fail_message,
                output_ref=materialized_output_ref,
                fail_flag_reason=verification_task_fail_reason(task_row, error_text=fail_message),
                answer_correct=parts.answer_correct,
            )
        _persist_verification_artifact(
            verification_id=verification_id,
            test_name=test_name,
            ref_key="input_ref",
            file_name=test_name,
            payload=output_blob,
        )
        return TaskExecutionResult(
            task_id=task_id,
            status=VerificationTaskStore.TASK_DONE,
            verdict=parts.verdict or "OK",
            run_id=run_id,
            judgehost_task_id=judgehost_task_id,
            runtime_sec=parts.runtime_sec,
            cpu_sec=parts.cpu_sec,
            wall_sec=parts.wall_sec,
            memory_kb=parts.memory_kb,
            compile_log=parts.compile_log,
            diagnostics_json=parts.diagnostics_json,
            error_text=parts.error_text,
            feedback_text=parts.feedback_text,
            output_ref=materialized_output_ref,
            answer_correct=parts.answer_correct,
        )

    task_status = VerificationTaskStore.TASK_DONE if result_status == Status.OK.value else VerificationTaskStore.TASK_FAILED
    fail_flag_reason = ""
    final_error = parts.error_text
    if task_kind == TASK_MAIN_CORRECT and task_status != VerificationTaskStore.TASK_DONE:
        final_error = _final_error_text(parts, fallback=error_text or f"main correct failed on {test_name}")
        fail_flag_reason = verification_task_fail_reason(task_row, error_text=final_error)
    if task_kind == TASK_MAIN_CORRECT and task_status == VerificationTaskStore.TASK_DONE:
        output_bytes = materialized_output_blob
        if output_bytes is None:
            fail_message = f"main correct output missing for {test_name}"
            return TaskExecutionResult(
                task_id=task_id,
                status=VerificationTaskStore.TASK_FAILED,
                verdict="FL",
                run_id=run_id,
                judgehost_task_id=judgehost_task_id,
                runtime_sec=parts.runtime_sec,
                cpu_sec=parts.cpu_sec,
                wall_sec=parts.wall_sec,
                memory_kb=parts.memory_kb,
                compile_log=parts.compile_log,
                diagnostics_json=parts.diagnostics_json,
                error_text=fail_message,
                feedback_text=parts.feedback_text,
                output_ref=materialized_output_ref,
                fail_flag_reason=verification_task_fail_reason(task_row, error_text=fail_message),
                answer_correct=parts.answer_correct,
            )
        _persist_verification_artifact(
            verification_id=verification_id,
            test_name=test_name,
            ref_key="answer_ref",
            file_name=_answer_name(test_name),
            payload=output_bytes,
        )
    return TaskExecutionResult(
        task_id=task_id,
        status=task_status,
        verdict=parts.verdict,
        run_id=run_id,
        judgehost_task_id=judgehost_task_id,
        runtime_sec=parts.runtime_sec,
        cpu_sec=parts.cpu_sec,
        wall_sec=parts.wall_sec,
        memory_kb=parts.memory_kb,
        compile_log=parts.compile_log,
        diagnostics_json=parts.diagnostics_json,
        error_text=final_error,
        feedback_text=parts.feedback_text,
        output_ref=materialized_output_ref,
        fail_flag_reason=fail_flag_reason,
        answer_correct=parts.answer_correct,
    )
