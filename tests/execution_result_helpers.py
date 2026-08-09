from __future__ import annotations

from collections.abc import Iterable

from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)


def execution_result(
    verdict: str,
    *,
    runtime_sec: float = 0.0,
    cpu_sec: float = 0.0,
    wall_sec: float = 0.0,
    memory_kb: int = 0,
    output_ref: str = "",
    feedback: str = "",
    error: str = "",
    diagnostics: Iterable[dict[str, object]] = (),
    answer_correct: bool | None = None,
) -> ExecutionResult:
    accepted = verdict.upper() in {"AC", "OK"} if answer_correct is None else answer_correct
    passes: tuple[ExecutionPassResult, ...] = ()
    if output_ref:
        runresult = (
            "correct"
            if accepted
            else {"TL": "timelimit"}.get(verdict.upper(), "wrong-answer")
        )
        passes = (
            ExecutionPassResult(
                number=1,
                capture_status=CAPTURE_COMPLETE,
                runresult=runresult,
                verdict=verdict,
                score_text="",
                answer_correct=accepted,
                usage=ExecutionUsage(
                    runtime_sec=runtime_sec,
                    cpu_sec=cpu_sec,
                    wall_sec=wall_sec,
                    memory_kb=memory_kb,
                ),
                feedback=feedback,
                artifacts=PassArtifacts(
                    input_ref=output_ref,
                    output_ref=output_ref,
                    stderr_ref=output_ref,
                    system_ref=output_ref,
                    judge_message_ref=output_ref,
                    team_message_ref=output_ref,
                    metadata_ref=output_ref,
                    compare_metadata_ref=output_ref,
                ),
            ),
        )
    return normalize_execution_result(
        passes=passes,
        verdict=verdict,
        answer_correct=accepted,
        error=error,
        feedback=feedback,
        compile_diagnostics=diagnostics,
    )
