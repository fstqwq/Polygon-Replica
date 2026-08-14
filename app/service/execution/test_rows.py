ExecutionTestPassRow = dict[str, object]
ExecutionTestRow = dict[str, object]


def _required_int(row: ExecutionTestPassRow, key: str) -> int:
    value = row[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"execution pass {key} must be an integer")
    return value


def build_execution_test_pass_row(
    *,
    verdict: str,
    time_ms: int | None = None,
    time_user_ms: int | None = None,
    time_wall_ms: int | None = None,
    memory_kb: int | None = None,
    feedback: str = "",
    capture_status: str = "",
    input_ref: str = "",
    output_ref: str = "",
    transcript_ref: str = "",
    judge_message_ref: str = "",
    runresult: str = "",
    pass_number: int = 1,
    answer_correct: bool = False,
) -> ExecutionTestPassRow:
    resolved_pass_number = int(pass_number)
    if resolved_pass_number < 1:
        raise ValueError("execution pass number must be positive")
    resolved_time_ms = max(0, int(0 if time_ms is None else time_ms))
    resolved_time_user_ms = max(0, int(resolved_time_ms if time_user_ms is None else time_user_ms))
    resolved_time_wall_ms = max(
        0, int(resolved_time_user_ms if time_wall_ms is None else time_wall_ms)
    )
    resolved_memory_kb = max(0, int(0 if memory_kb is None else memory_kb))
    row: ExecutionTestPassRow = {
        "pass": resolved_pass_number,
        "verdict": verdict or "",
        "time_ms": resolved_time_ms,
        "time_user_ms": resolved_time_user_ms,
        "time_wall_ms": resolved_time_wall_ms,
        "memory_kb": resolved_memory_kb,
        "feedback": feedback or "",
        "capture_status": capture_status or "",
        "input_ref": input_ref or "",
        "output_ref": output_ref or "",
        "transcript_ref": transcript_ref or "",
        "judge_message_ref": judge_message_ref or "",
        "answer_correct": bool(answer_correct),
    }
    if runresult:
        row["runresult"] = runresult
    return row


def build_execution_test_row(
    *,
    test_name: str,
    verdict: str = "",
    time_ms: int | None = None,
    time_user_ms: int | None = None,
    time_wall_ms: int | None = None,
    memory_kb: int | None = None,
    message: str = "",
    output_ref: str = "",
    feedback_files: list[str] | None = None,
    passes: list[ExecutionTestPassRow] | None = None,
    runresult: str = "",
    answer_correct: bool = False,
) -> ExecutionTestRow:
    canonical_passes = list(passes or [])
    if not canonical_passes:
        canonical_passes = [
            build_execution_test_pass_row(
                verdict=verdict,
                time_ms=time_ms,
                time_user_ms=time_user_ms,
                time_wall_ms=time_wall_ms,
                memory_kb=memory_kb,
                feedback=message,
                output_ref=output_ref,
                runresult=runresult,
                answer_correct=answer_correct,
            )
        ]
    final_pass = canonical_passes[-1]
    resolved_verdict = verdict or str(final_pass.get("verdict") or "")
    resolved_time_ms = max(
        0,
        _required_int(final_pass, "time_ms") if time_ms is None else time_ms,
    )
    resolved_time_user_ms = max(
        0,
        (_required_int(final_pass, "time_user_ms") if time_user_ms is None else time_user_ms),
    )
    resolved_time_wall_ms = max(
        0,
        (_required_int(final_pass, "time_wall_ms") if time_wall_ms is None else time_wall_ms),
    )
    resolved_memory_kb = max(
        0,
        _required_int(final_pass, "memory_kb") if memory_kb is None else memory_kb,
    )
    resolved_message = message or str(final_pass.get("feedback") or "")
    resolved_output_ref = output_ref or str(final_pass.get("output_ref") or "")
    row: ExecutionTestRow = {
        "test": test_name,
        "verdict": resolved_verdict,
        "time_ms": resolved_time_ms,
        "time_user_ms": resolved_time_user_ms,
        "time_wall_ms": resolved_time_wall_ms,
        "memory_kb": resolved_memory_kb,
        "message": resolved_message,
        "output_ref": resolved_output_ref,
        "feedback_files": list(feedback_files or []),
        "passes": canonical_passes,
        "answer_correct": bool(answer_correct or final_pass.get("answer_correct")),
    }
    if runresult:
        row["runresult"] = runresult
    return row
