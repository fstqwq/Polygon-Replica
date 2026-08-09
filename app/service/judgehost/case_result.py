from __future__ import annotations

from app.service.verification.test_rows import (
    build_verification_test_pass_row,
    build_verification_test_row,
)
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)


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
    _ = feedback_files
    return normalize_execution_result(
        passes=(*historical_passes, final_pass),
        verdict=verdict,
        score_text=score_text,
        answer_correct=answer_correct,
        feedback=feedback_text,
        warnings=warnings,
    )


def decode_case_test_row(result: ExecutionResult, *, test_name: str) -> dict[str, object]:
    passes = [
        build_verification_test_pass_row(
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
    return build_verification_test_row(
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
