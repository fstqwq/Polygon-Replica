from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.service.judgehost.batch_scheduler_models import CaseResult
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.pass_bundle import PassBundle
from app.service.judgehost.runtime import (
    domjudge_feedback_text_and_files,
    domjudge_feedback_text_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
    domjudge_verdict_from_runresult,
)
from app.service.judgehost.shared import domjudge_lower_text, domjudge_text
from app.service.verification.execution_result import (
    ExecutionPassResult,
    ExecutionUsage,
    PassArtifacts,
)


_PASS_CACHE_FILE_NAMES = {
    "input": "input",
    "program.out": "program-out",
    "program.err": "program-err",
    "system.out": "system-out",
    "program.meta": "program-meta",
    "compare.meta": "compare-meta",
    "judgemessage.txt": "judgemessage",
    "teammessage.txt": "teammessage",
}


def pass_cache_file_name(number: int, name: str) -> str:
    return f"pass-{number}-{_PASS_CACHE_FILE_NAMES[name]}"


@dataclass(frozen=True)
class CapturedCaseArtifact:
    content: bytes
    blob_ref: str


@dataclass(frozen=True)
class CapturedJudgehostCase:
    test_name: str
    input_ref: str
    interactive: bool
    raw_runresult: str
    runtime_fallback_sec: float
    score_text: str
    run_config: Mapping[str, object]
    artifacts: Mapping[str, CapturedCaseArtifact]
    pass_bundle: PassBundle | None
    capture_warning: str = ""
    debug_text: str = ""


@dataclass(frozen=True)
class NormalizedJudgehostCase:
    result: CaseResult
    runresult: str
    verdict: str
    runtime_sec: float
    cpu_sec: float
    wall_sec: float
    memory_kb: int
    score_text: str


def _answer_correct_from_compare_exit_code(compare_exit_code: int) -> bool:
    return int(compare_exit_code) == 42


def _optional_meta_float(meta: Mapping[str, str], key: str) -> float | None:
    raw = meta.get(key)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _program_meta_usage(payload: bytes) -> ExecutionUsage:
    meta = domjudge_parse_meta_text(payload.decode("utf-8", errors="replace"))
    time_used_key = meta.get("time-used")
    runtime_sec = _optional_meta_float(meta, "time-used")
    if runtime_sec is None and time_used_key in meta:
        runtime_sec = _optional_meta_float(meta, time_used_key)
    memory_raw = meta.get("memory-bytes")
    memory_kb: int | None = None
    if memory_raw is not None:
        try:
            memory_kb = max(0, int(memory_raw) // 1024)
        except (TypeError, ValueError):
            memory_kb = None
    return ExecutionUsage(
        runtime_sec=runtime_sec,
        cpu_sec=_optional_meta_float(meta, "cpu-time"),
        wall_sec=_optional_meta_float(meta, "wall-time"),
        memory_kb=memory_kb,
    )


def normalize_captured_case(
    captured: CapturedJudgehostCase,
    *,
    limit_bytes: int,
) -> NormalizedJudgehostCase:
    artifacts = captured.artifacts

    def _content(name: str) -> bytes:
        artifact = artifacts.get(name)
        return b"" if artifact is None else artifact.content

    def _ref(name: str) -> str:
        artifact = artifacts.get(name)
        return "" if artifact is None else artifact.blob_ref

    refs_to_content = {
        artifact.blob_ref: artifact.content
        for artifact in artifacts.values()
        if artifact.blob_ref
    }

    metadata_blob = _content("program.meta")
    compare_meta_blob = _content("compare.meta")
    runtime_sec = max(0.0, float(captured.runtime_fallback_sec))
    cpu_sec = runtime_sec
    wall_sec = runtime_sec
    memory_kb = 0
    compare_exit_code = -1
    program_meta: dict[str, str] = {}
    if metadata_blob:
        program_meta = domjudge_parse_meta_text(
            metadata_blob.decode("utf-8", errors="replace")
        )
        cpu_total_sec = domjudge_parse_float(
            program_meta.get("cpu-time"),
            runtime_sec,
        )
        wall_sec = domjudge_parse_float(
            program_meta.get("wall-time"),
            cpu_total_sec,
        )
        runtime_sec = cpu_sec = cpu_total_sec
        mem_bytes = domjudge_parse_int(program_meta.get("memory-bytes"), 0)
        memory_kb = max(0, int(mem_bytes // 1024))
    if compare_meta_blob:
        compare_meta = domjudge_parse_meta_text(
            compare_meta_blob.decode("utf-8", errors="replace")
        )
        compare_exit_code = domjudge_parse_int(compare_meta.get("exitcode"), -1)

    runresult = domjudge_rewrite_untrusted_runresult(
        domjudge_lower_text(captured.raw_runresult, default="internal-error"),
        cpu_sec=cpu_sec,
        run_cfg_obj=dict(captured.run_config),
    )
    if (
        runresult in {"compare-error", "run-error", "internal-error"}
        and compare_exit_code < 0
    ):
        time_result = domjudge_lower_text(program_meta.get("time-result"))
        signal_num = domjudge_parse_int(program_meta.get("signal"), 0)
        output_limit_kb = domjudge_parse_int(
            captured.run_config.get("output_limit"),
            0,
        )
        output_limit_bytes = max(0, int(output_limit_kb) * 1024)
        stdout_bytes = domjudge_parse_int(program_meta.get("stdout-bytes"), 0)
        output_truncated = domjudge_lower_text(
            program_meta.get("output-truncated")
        )
        timed_out = "timelimit" in time_result or signal_num == 14
        output_limited = (
            output_limit_bytes > 0 and stdout_bytes >= output_limit_bytes
        ) or (
            output_truncated in {"1", "true", "yes", "on"}
            and stdout_bytes > 0
        )
        if timed_out:
            runresult = "timelimit"
        elif output_limited:
            runresult = "output-limit"
    if runresult in {"compare-error", "run-error"} and compare_exit_code == 3:
        runresult = "checker-fail"

    verdict = domjudge_verdict_from_runresult(runresult)
    output_run_ref = _ref("program.out")
    output_error_ref = _ref("program.err")
    output_system_ref = _ref("system.out")
    output_diff_ref = _ref("judgemessage.txt")
    metadata_ref = _ref("program.meta")
    compare_metadata_ref = _ref("compare.meta")
    team_message_ref = _ref("teammessage.txt")
    feedback_text, feedback_files = domjudge_feedback_text_and_files(
        read_blob=refs_to_content.get,
        runresult=runresult,
        output_error_ref=output_error_ref,
        output_diff_ref=output_diff_ref,
        team_message_ref=team_message_ref,
        limit_bytes=limit_bytes,
    )
    debug_feedback = domjudge_feedback_text_from_text(
        captured.debug_text,
        limit_bytes=limit_bytes,
    )
    if debug_feedback:
        if not feedback_text:
            feedback_text = debug_feedback
        elif debug_feedback not in feedback_text:
            feedback_text = f"{feedback_text}\n{debug_feedback}"

    def _bundled_ref(number: int, name: str) -> str:
        if (
            captured.pass_bundle is not None
            and number == captured.pass_bundle.final_pass_number
            and name == "teammessage.txt"
        ):
            return team_message_ref
        return _ref(pass_cache_file_name(number, name))

    historical_passes: list[ExecutionPassResult] = []
    incomplete_metadata_passes: list[int] = []
    final_pass_number = 1
    final_input_ref = captured.input_ref
    if captured.pass_bundle is not None:
        final_pass_number = captured.pass_bundle.final_pass_number
        final_input_ref = _bundled_ref(final_pass_number, "input")
        for bundled_pass in captured.pass_bundle.passes[:-1]:
            files = bundled_pass.files
            usage = _program_meta_usage(files["program.meta"])
            compare_meta = domjudge_parse_meta_text(
                files["compare.meta"].decode("utf-8", errors="replace")
            )
            historical_compare_exit = domjudge_parse_int(
                compare_meta.get("exitcode"),
                -1,
            )
            if (
                None
                in (
                    usage.runtime_sec,
                    usage.cpu_sec,
                    usage.wall_sec,
                    usage.memory_kb,
                )
                or historical_compare_exit < 0
            ):
                incomplete_metadata_passes.append(bundled_pass.number)
            historical_judge_ref = _bundled_ref(
                bundled_pass.number,
                "judgemessage.txt",
            )
            historical_team_ref = _bundled_ref(
                bundled_pass.number,
                "teammessage.txt",
            )
            historical_error_ref = _bundled_ref(
                bundled_pass.number,
                "program.err",
            )
            historical_feedback, _historical_feedback_files = (
                domjudge_feedback_text_and_files(
                    read_blob=refs_to_content.get,
                    runresult="correct",
                    output_error_ref=historical_error_ref,
                    output_diff_ref=historical_judge_ref,
                    team_message_ref=historical_team_ref,
                    limit_bytes=limit_bytes,
                )
            )
            historical_passes.append(
                ExecutionPassResult(
                    number=bundled_pass.number,
                    capture_status=bundled_pass.capture_status,
                    runresult="correct",
                    verdict="OK",
                    score_text="",
                    answer_correct=_answer_correct_from_compare_exit_code(
                        historical_compare_exit
                    ),
                    usage=usage,
                    feedback=historical_feedback,
                    artifacts=PassArtifacts(
                        input_ref=_bundled_ref(
                            bundled_pass.number,
                            "input",
                        ),
                        output_ref=(
                            ""
                            if captured.interactive
                            else _bundled_ref(
                                bundled_pass.number,
                                "program.out",
                            )
                        ),
                        transcript_ref=(
                            _bundled_ref(
                                bundled_pass.number,
                                "program.out",
                            )
                            if captured.interactive
                            else ""
                        ),
                        stderr_ref=historical_error_ref,
                        system_ref=_bundled_ref(
                            bundled_pass.number,
                            "system.out",
                        ),
                        judge_message_ref=historical_judge_ref,
                        team_message_ref=historical_team_ref,
                        metadata_ref=_bundled_ref(
                            bundled_pass.number,
                            "program.meta",
                        ),
                        compare_metadata_ref=_bundled_ref(
                            bundled_pass.number,
                            "compare.meta",
                        ),
                    ),
                )
            )

    capture_warning = captured.capture_warning
    if incomplete_metadata_passes:
        metadata_warning = (
            "pass metadata missing for passes "
            + ", ".join(
                str(number) for number in incomplete_metadata_passes
            )
        )
        capture_warning = (
            f"{capture_warning}; {metadata_warning}"
            if capture_warning
            else "historical pass artifact capture was incomplete: "
            + metadata_warning
        )
    result = build_case_result(
        test_name=domjudge_text(captured.test_name),
        runresult=runresult,
        verdict=verdict,
        runtime_sec=runtime_sec,
        cpu_sec=cpu_sec,
        wall_sec=wall_sec,
        memory_kb=memory_kb,
        score_text=domjudge_text(captured.score_text),
        output_run_ref=output_run_ref,
        output_error_ref=output_error_ref,
        output_system_ref=output_system_ref,
        output_diff_ref=output_diff_ref,
        metadata_ref=metadata_ref,
        compare_metadata_ref=compare_metadata_ref,
        team_message_ref=team_message_ref,
        feedback_text=feedback_text,
        feedback_files=feedback_files,
        answer_correct=_answer_correct_from_compare_exit_code(
            compare_exit_code
        ),
        input_ref=final_input_ref,
        interactive=captured.interactive,
        pass_number=final_pass_number,
        historical_passes=tuple(historical_passes),
        warnings=() if not capture_warning else (capture_warning,),
        usage=(
            _program_meta_usage(metadata_blob)
            if metadata_blob
            else ExecutionUsage()
        ),
    )
    return NormalizedJudgehostCase(
        result=result,
        runresult=runresult,
        verdict=verdict,
        runtime_sec=runtime_sec,
        cpu_sec=cpu_sec,
        wall_sec=wall_sec,
        memory_kb=memory_kb,
        score_text=domjudge_text(captured.score_text),
    )
