from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.service.verification.task_metadata import canonical_diagnostics, canonical_truncated_text, diagnostics_json_text
from app.service.verification.task_scheduler import TaskExecutionResult
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.test_rows import build_verification_test_row
from app.service.verification.types import Status

TASK_GENERATE_INPUT = "generate-input"
TASK_MAIN_CORRECT = "main-correct"

_COMPILE_LOG_CHAR_LIMIT = 16384
_ERROR_TEXT_CHAR_LIMIT = 4096
_FEEDBACK_TEXT_CHAR_LIMIT = 4096
_COMPILE_DIAGNOSTICS_LIMIT = 64


@dataclass(frozen=True)
class _TaskSummaryParts:
    verdict: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    compile_log: str
    compile_log_truncated: bool
    compile_log_total_chars: int
    diagnostics_json: str
    diagnostics_truncated: bool
    diagnostics_total: int
    error_text: str
    error_text_truncated: bool
    error_text_total_chars: int
    feedback_text: str
    feedback_text_truncated: bool
    feedback_text_total_chars: int
    output_ref: str


def _answer_name(test_name: str) -> str:
    stem = Path(test_name).stem
    if not stem:
        raise RuntimeError("test name is required")
    return f"{stem}.ans"


def _resolve_output_blob(output_ref: str) -> bytes | None:
    if not output_ref:
        return None
    try:
        from app.impl.runtime.config import config
        return config.judgehost_task_service.resolve_artifact_blob(output_ref)
    except Exception:
        return None


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
    compile_log_source = str(summary.get("error") or error_text or "")
    compile_log_meta = canonical_truncated_text(compile_log_source, limit=_COMPILE_LOG_CHAR_LIMIT)
    diagnostics_meta = canonical_diagnostics(
        cast(list[dict[str, object]] | list[object] | None, summary.get("compile_diagnostics") or []),
        list_limit=_COMPILE_DIAGNOSTICS_LIMIT,
        message_limit=_ERROR_TEXT_CHAR_LIMIT,
    )
    diagnostics_json = diagnostics_json_text(diagnostics_meta["rows"])
    error_value = str(error_text or summary.get("error") or "")
    error_text_meta = canonical_truncated_text(error_value, limit=_ERROR_TEXT_CHAR_LIMIT)
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    feedback_source = ""
    output_ref = ""
    runtime_sec: float | None = None
    cpu_sec: float | None = None
    wall_sec: float | None = None
    memory_kb: int | None = None
    if tests:
        test_row = dict(tests[-1])
        feedback_source = str(test_row.get("message") or "")
        output_ref = str(test_row.get("output_ref") or "")
        cpu_ms = int(test_row.get("time_user_ms") or test_row.get("time_ms") or 0)
        wall_ms = int(test_row.get("time_wall_ms") or cpu_ms)
        runtime_ms = int(test_row.get("time_ms") or cpu_ms)
        memory_kb = int(test_row.get("memory_kb") or 0)
        runtime_sec = float(runtime_ms) / 1000.0
        cpu_sec = float(cpu_ms) / 1000.0
        wall_sec = float(wall_ms) / 1000.0
    feedback_meta = canonical_truncated_text(feedback_source, limit=_FEEDBACK_TEXT_CHAR_LIMIT)
    return _TaskSummaryParts(
        verdict=verdict,
        runtime_sec=runtime_sec,
        cpu_sec=cpu_sec,
        wall_sec=wall_sec,
        memory_kb=memory_kb,
        compile_log=compile_log_meta["text"],
        compile_log_truncated=bool(compile_log_meta["truncated"]),
        compile_log_total_chars=int(compile_log_meta["total_chars"]),
        diagnostics_json=diagnostics_json,
        diagnostics_truncated=bool(diagnostics_meta["truncated"]),
        diagnostics_total=int(diagnostics_meta["total"]),
        error_text=error_text_meta["text"],
        error_text_truncated=bool(error_text_meta["truncated"]),
        error_text_total_chars=int(error_text_meta["total_chars"]),
        feedback_text=feedback_meta["text"],
        feedback_text_truncated=bool(feedback_meta["truncated"]),
        feedback_text_total_chars=int(feedback_meta["total_chars"]),
        output_ref=output_ref,
    )


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
    )


def finalize_verification_task_result(task_row: VerificationTaskRow, *, result: dict[str, object]) -> TaskExecutionResult:
    from app.impl.runtime.config import config

    verification_id = str(task_row["verification_id"] or "")
    layout = config.fs_manager.prepare_verification_layout(verification_id)
    task_id = str(task_row["id"] or "")
    task_kind = str(task_row["task_kind"] or "")
    test_name = str(task_row["test_name"] or "")
    judgehost_task_id = str(task_row["judgehost_task_id"] or "")
    run_id = str(result.get("run_id") or str(task_row["run_id"] or ""))
    result_summary = dict(result.get("summary") or {})
    result_status = str(result.get("status") or "")
    error_text = str(result.get("error") or result_summary.get("error") or "")
    missing_case_result = bool(result.get("missing_case_result"))
    parts = _summary_parts(result_summary, run_status=result_status, error_text=error_text)

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
            compile_log_truncated=parts.compile_log_truncated,
            compile_log_total_chars=parts.compile_log_total_chars,
            diagnostics_json=parts.diagnostics_json,
            diagnostics_truncated=parts.diagnostics_truncated,
            diagnostics_total=parts.diagnostics_total,
            error_text=parts.error_text,
            error_text_truncated=parts.error_text_truncated,
            error_text_total_chars=parts.error_text_total_chars,
            feedback_text="",
            feedback_text_truncated=False,
            feedback_text_total_chars=0,
            output_ref="",
            fail_flag_reason=fail_reason if task_kind == TASK_MAIN_CORRECT else "",
        )

    if task_kind == TASK_GENERATE_INPUT:
        if result_status != Status.OK.value:
            fail_reason = error_text or f"generate-input failed on {test_name}"
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
                compile_log_truncated=parts.compile_log_truncated,
                compile_log_total_chars=parts.compile_log_total_chars,
                diagnostics_json=parts.diagnostics_json,
                diagnostics_truncated=parts.diagnostics_truncated,
                diagnostics_total=parts.diagnostics_total,
                error_text=parts.error_text,
                error_text_truncated=parts.error_text_truncated,
                error_text_total_chars=parts.error_text_total_chars,
                feedback_text=parts.feedback_text,
                feedback_text_truncated=parts.feedback_text_truncated,
                feedback_text_total_chars=parts.feedback_text_total_chars,
                output_ref=parts.output_ref,
                fail_flag_reason=fail_reason,
            )
        output_blob = _resolve_output_blob(parts.output_ref)
        target_path = layout.tests / test_name
        if output_blob is None:
            if not target_path.exists():
                fail_message = f"generated input output missing for {test_name}"
                fail_meta = canonical_truncated_text(fail_message, limit=_ERROR_TEXT_CHAR_LIMIT)
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
                    compile_log_truncated=parts.compile_log_truncated,
                    compile_log_total_chars=parts.compile_log_total_chars,
                    diagnostics_json=parts.diagnostics_json,
                    diagnostics_truncated=parts.diagnostics_truncated,
                    diagnostics_total=parts.diagnostics_total,
                    error_text=fail_meta["text"],
                    error_text_truncated=bool(fail_meta["truncated"]),
                    error_text_total_chars=int(fail_meta["total_chars"]),
                    feedback_text=parts.feedback_text,
                    feedback_text_truncated=parts.feedback_text_truncated,
                    feedback_text_total_chars=parts.feedback_text_total_chars,
                    output_ref=parts.output_ref,
                    fail_flag_reason=fail_message,
                )
        else:
            target_path.write_bytes(output_blob)
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
            compile_log_truncated=parts.compile_log_truncated,
            compile_log_total_chars=parts.compile_log_total_chars,
            diagnostics_json=parts.diagnostics_json,
            diagnostics_truncated=parts.diagnostics_truncated,
            diagnostics_total=parts.diagnostics_total,
            error_text=parts.error_text,
            error_text_truncated=parts.error_text_truncated,
            error_text_total_chars=parts.error_text_total_chars,
            feedback_text=parts.feedback_text,
            feedback_text_truncated=parts.feedback_text_truncated,
            feedback_text_total_chars=parts.feedback_text_total_chars,
            output_ref=parts.output_ref,
        )

    task_status = VerificationTaskStore.TASK_DONE if result_status == Status.OK.value else VerificationTaskStore.TASK_FAILED
    fail_flag_reason = ""
    if task_kind == TASK_MAIN_CORRECT and task_status != VerificationTaskStore.TASK_DONE:
        fail_flag_reason = error_text or f"main correct failed on {test_name}"
    if task_kind == TASK_MAIN_CORRECT and task_status == VerificationTaskStore.TASK_DONE:
        output_bytes = _resolve_output_blob(parts.output_ref)
        if output_bytes is None:
            fail_message = f"main correct output missing for {test_name}"
            fail_meta = canonical_truncated_text(fail_message, limit=_ERROR_TEXT_CHAR_LIMIT)
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
                compile_log_truncated=parts.compile_log_truncated,
                compile_log_total_chars=parts.compile_log_total_chars,
                diagnostics_json=parts.diagnostics_json,
                diagnostics_truncated=parts.diagnostics_truncated,
                diagnostics_total=parts.diagnostics_total,
                error_text=fail_meta["text"],
                error_text_truncated=bool(fail_meta["truncated"]),
                error_text_total_chars=int(fail_meta["total_chars"]),
                feedback_text=parts.feedback_text,
                feedback_text_truncated=parts.feedback_text_truncated,
                feedback_text_total_chars=parts.feedback_text_total_chars,
                output_ref=parts.output_ref,
                fail_flag_reason=fail_message,
            )
        (layout.ans / _answer_name(test_name)).write_bytes(output_bytes)
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
        compile_log_truncated=parts.compile_log_truncated,
        compile_log_total_chars=parts.compile_log_total_chars,
        diagnostics_json=parts.diagnostics_json,
        diagnostics_truncated=parts.diagnostics_truncated,
        diagnostics_total=parts.diagnostics_total,
        error_text=parts.error_text,
        error_text_truncated=parts.error_text_truncated,
        error_text_total_chars=parts.error_text_total_chars,
        feedback_text=parts.feedback_text,
        feedback_text_truncated=parts.feedback_text_truncated,
        feedback_text_total_chars=parts.feedback_text_total_chars,
        output_ref=parts.output_ref,
        fail_flag_reason=fail_flag_reason,
    )
