from dataclasses import replace
from typing import TypedDict, cast

from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CompileResult,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import (
    canonical_compile_diagnostics,
    canonical_execution_result,
    normalize_execution_result,
)
from app.service.execution.test_rows import (
    build_execution_test_pass_row,
    build_execution_test_row,
)


class CaseTerminalReport(TypedDict):
    task_id: str
    verification_id: str
    run_id: str
    artifact_path: str
    status: str
    task_status: str
    error: str
    summary: dict[str, object]
    missing_case_result: bool
    execution_result: ExecutionResult


def execution_result_with_terminal_context(
    result: ExecutionResult,
    *,
    summary: dict[str, object],
    error_text: str,
) -> ExecutionResult:
    """Attach task-level compile/error context without rebuilding pass evidence."""
    summary_diagnostics = cast(
        list[dict[str, object]],
        summary.get("compile_diagnostics") or [],
    )
    diagnostics = (
        result.compile.diagnostics
        if result.compile.diagnostics
        else canonical_compile_diagnostics(summary_diagnostics)
    )
    summary_error = str(summary.get("error") or "")
    compile_log = result.compile.log
    if not compile_log and diagnostics:
        compile_log = str(summary.get("compile_log") or summary_error or error_text)
    resolved_error = result.outcome.error or error_text or summary_error
    return canonical_execution_result(
        replace(
            result,
            outcome=replace(result.outcome, error=resolved_error),
            compile=CompileResult(log=compile_log, diagnostics=diagnostics),
        )
    )


def build_missing_case_result(
    *,
    summary: dict[str, object],
    error_text: str,
) -> ExecutionResult:
    diagnostics = cast(
        list[dict[str, object]],
        summary.get("compile_diagnostics") or [],
    )
    return normalize_execution_result(
        verdict="CE" if diagnostics else "FL",
        error=error_text,
        compile_log=(
            str(summary.get("compile_log") or summary.get("error") or error_text)
            if diagnostics
            else ""
        ),
        compile_diagnostics=diagnostics,
        warnings=cast(list[str], summary.get("warnings") or []),
    )


def build_case_terminal_report(
    *,
    task_id: str,
    verification_id: str,
    run_id: str,
    status: str,
    task_status: str,
    error_text: str,
    summary: dict[str, object],
    missing_case_result: bool,
    execution_result: ExecutionResult,
) -> CaseTerminalReport:
    return {
        "task_id": task_id,
        "verification_id": verification_id,
        "run_id": run_id,
        "artifact_path": "",
        "status": status,
        "task_status": task_status,
        "error": error_text,
        "summary": summary,
        "missing_case_result": missing_case_result,
        "execution_result": execution_result,
    }


def build_case_result(
    *,
    test_name: str,
    runresult: str,
    verdict: str,
    runtime_sec: float,
    cpu_sec: float,
    wall_sec: float,
    memory_kb: int,
    score_text: str,
    output_run_ref: str,
    output_error_ref: str,
    output_system_ref: str,
    output_diff_ref: str,
    metadata_ref: str,
    compare_metadata_ref: str,
    team_message_ref: str,
    feedback_text: str,
    feedback_files: list[str] | tuple[str, ...],
    answer_correct: bool,
    input_ref: str = "",
    interactive: bool = False,
    pass_number: int = 1,
    historical_passes: tuple[ExecutionPassResult, ...] = (),
    warnings: tuple[str, ...] = (),
    usage: ExecutionUsage | None = None,
    compile_log: str = "",
    compile_diagnostics: tuple[dict[str, object], ...] = (),
) -> ExecutionResult:
    resolved_usage = usage or ExecutionUsage(
        runtime_sec=max(0.0, float(runtime_sec)),
        cpu_sec=max(0.0, float(cpu_sec)),
        wall_sec=max(0.0, float(wall_sec)),
        memory_kb=max(0, int(memory_kb)),
    )
    if runresult == "compiler-error" or not (
        input_ref
        and output_run_ref
        and metadata_ref
        and compare_metadata_ref
        and output_error_ref
        and output_system_ref
        and output_diff_ref
        and team_message_ref
    ):
        return normalize_execution_result(
            verdict=verdict,
            score_text=score_text,
            answer_correct=answer_correct,
            feedback=feedback_text,
            compile_log=compile_log,
            compile_diagnostics=compile_diagnostics,
            warnings=warnings,
        )
    final_pass = ExecutionPassResult(
        number=int(pass_number),
        capture_status=CAPTURE_COMPLETE,
        runresult=runresult,
        verdict=verdict,
        score_text=score_text,
        answer_correct=bool(answer_correct),
        usage=resolved_usage,
        feedback=feedback_text,
        artifacts=PassArtifacts(
            input_ref=input_ref,
            output_ref="" if interactive else output_run_ref,
            transcript_ref=output_run_ref if interactive else "",
            stderr_ref=output_error_ref,
            system_ref=output_system_ref,
            judge_message_ref=output_diff_ref,
            team_message_ref=team_message_ref,
            metadata_ref=metadata_ref,
            compare_metadata_ref=compare_metadata_ref,
        ),
    )
    return normalize_execution_result(
        passes=(*historical_passes, final_pass),
        verdict=verdict,
        score_text=score_text,
        answer_correct=answer_correct,
        feedback=feedback_text,
        compile_log=compile_log,
        compile_diagnostics=compile_diagnostics,
        warnings=warnings,
    )


def decode_case_test_row(result: ExecutionResult, *, test_name: str) -> dict[str, object]:
    passes = [
        build_execution_test_pass_row(
            verdict=pass_result.verdict,
            time_ms=(
                0
                if pass_result.usage.runtime_sec is None
                else int(round(pass_result.usage.runtime_sec * 1000.0))
            ),
            time_user_ms=(
                0
                if pass_result.usage.cpu_sec is None
                else int(round(pass_result.usage.cpu_sec * 1000.0))
            ),
            time_wall_ms=(
                0
                if pass_result.usage.wall_sec is None
                else int(round(pass_result.usage.wall_sec * 1000.0))
            ),
            memory_kb=pass_result.usage.memory_kb,
            feedback=pass_result.feedback,
            capture_status=pass_result.capture_status,
            input_ref=pass_result.artifacts.input_ref,
            output_ref=pass_result.artifacts.output_ref,
            transcript_ref=pass_result.artifacts.transcript_ref,
            judge_message_ref=pass_result.artifacts.judge_message_ref,
            runresult=pass_result.runresult,
            pass_number=pass_result.number,
            answer_correct=pass_result.answer_correct,
        )
        for pass_result in result.passes
    ]
    usage = result.outcome.usage
    return build_execution_test_row(
        test_name=test_name,
        verdict=result.outcome.verdict,
        time_ms=0 if usage.runtime_sec is None else int(round(usage.runtime_sec * 1000.0)),
        time_user_ms=0 if usage.cpu_sec is None else int(round(usage.cpu_sec * 1000.0)),
        time_wall_ms=0 if usage.wall_sec is None else int(round(usage.wall_sec * 1000.0)),
        memory_kb=usage.memory_kb,
        message=result.outcome.feedback,
        output_ref=result.output_run_ref,
        feedback_files=list(result.feedback_files),
        passes=passes,
        runresult=result.runresult,
        answer_correct=result.answer_correct,
    )
