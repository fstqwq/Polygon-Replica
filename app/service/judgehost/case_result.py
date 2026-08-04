from __future__ import annotations

import json
from typing import cast

from app.service.platform.hashing import canonical_json
from app.service.verification.test_rows import (
    build_verification_test_pass_row,
    build_verification_test_row,
)

from app.service.judgehost.batch_scheduler_models import CaseResult


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
) -> CaseResult:
    resolved_runtime = max(0.0, float(runtime_sec))
    resolved_cpu = max(0.0, float(cpu_sec if cpu_sec > 0.0 else resolved_runtime))
    resolved_wall = max(0.0, float(wall_sec if wall_sec > 0.0 else resolved_cpu))
    resolved_memory = max(0, int(memory_kb))
    time_ms = int(round(resolved_runtime * 1000.0))
    time_user_ms = int(round(resolved_cpu * 1000.0))
    time_wall_ms = int(round(resolved_wall * 1000.0))
    feedback_file_tokens = tuple(str(item) for item in feedback_files if str(item))
    test_row = build_verification_test_row(
        test_name=test_name,
        verdict=verdict,
        time_ms=time_ms,
        time_user_ms=time_user_ms,
        time_wall_ms=time_wall_ms,
        memory_kb=resolved_memory,
        message=feedback_text,
        output_ref=output_run_ref,
        feedback_files=list(feedback_file_tokens),
        passes=[
            build_verification_test_pass_row(
                verdict=verdict,
                time_ms=time_ms,
                time_user_ms=time_user_ms,
                time_wall_ms=time_wall_ms,
                memory_kb=resolved_memory,
                feedback=feedback_text,
                output_ref=output_run_ref,
                runresult=runresult,
                answer_correct=answer_correct,
            )
        ],
        runresult=runresult,
        answer_correct=answer_correct,
    )
    return CaseResult(
        runresult=runresult,
        verdict=verdict,
        runtime_sec=resolved_runtime,
        cpu_sec=resolved_cpu,
        wall_sec=resolved_wall,
        memory_kb=resolved_memory,
        score_text=score_text,
        output_run_ref=output_run_ref,
        output_error_ref=output_error_ref,
        output_system_ref=output_system_ref,
        output_diff_ref=output_diff_ref,
        metadata_ref=metadata_ref,
        compare_metadata_ref=compare_metadata_ref,
        team_message_ref=team_message_ref,
        feedback_text=feedback_text,
        feedback_files=feedback_file_tokens,
        answer_correct=bool(answer_correct),
        test_row_json=canonical_json(test_row, ensure_ascii=False),
    )


def decode_case_test_row(result: CaseResult) -> dict[str, object]:
    return cast(dict[str, object], json.loads(result.test_row_json))
