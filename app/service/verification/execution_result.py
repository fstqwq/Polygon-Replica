from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Iterable, cast

from app.service.platform.hashing import canonical_json


CAPTURE_COMPLETE = "complete"
CAPTURE_METADATA_INPUT_ONLY = "metadata-input-only"
CAPTURE_METADATA_ONLY = "metadata-only"
CAPTURE_STATUSES = frozenset(
    {
        CAPTURE_COMPLETE,
        CAPTURE_METADATA_INPUT_ONLY,
        CAPTURE_METADATA_ONLY,
    }
)


@dataclass(frozen=True)
class ExecutionUsage:
    runtime_sec: float | None = None
    cpu_sec: float | None = None
    wall_sec: float | None = None
    memory_kb: int | None = None


@dataclass(frozen=True)
class PassArtifacts:
    input_ref: str = ""
    output_ref: str = ""
    transcript_ref: str = ""
    stderr_ref: str = ""
    system_ref: str = ""
    judge_message_ref: str = ""
    team_message_ref: str = ""
    metadata_ref: str = ""
    compare_metadata_ref: str = ""

    def refs(self) -> tuple[str, ...]:
        return tuple(
            token
            for token in (
                self.input_ref,
                self.output_ref,
                self.transcript_ref,
                self.stderr_ref,
                self.system_ref,
                self.judge_message_ref,
                self.team_message_ref,
                self.metadata_ref,
                self.compare_metadata_ref,
            )
            if token
        )


@dataclass(frozen=True)
class ExecutionPassResult:
    number: int
    capture_status: str
    runresult: str
    verdict: str
    score_text: str
    answer_correct: bool
    usage: ExecutionUsage
    feedback: str
    artifacts: PassArtifacts


@dataclass(frozen=True)
class ExecutionOutcome:
    verdict: str = ""
    score_text: str = ""
    answer_correct: bool = False
    usage: ExecutionUsage = ExecutionUsage()
    error: str = ""
    feedback: str = ""


@dataclass(frozen=True)
class CompileResult:
    log: str = ""
    diagnostics: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome = ExecutionOutcome()
    compile: CompileResult = CompileResult()
    passes: tuple[ExecutionPassResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def final_pass(self) -> ExecutionPassResult | None:
        return None if not self.passes else self.passes[-1]

    @property
    def runresult(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.runresult

    @property
    def verdict(self) -> str:
        return self.outcome.verdict

    @property
    def runtime_sec(self) -> float | None:
        return self.outcome.usage.runtime_sec

    @property
    def cpu_sec(self) -> float | None:
        return self.outcome.usage.cpu_sec

    @property
    def wall_sec(self) -> float | None:
        return self.outcome.usage.wall_sec

    @property
    def memory_kb(self) -> int | None:
        return self.outcome.usage.memory_kb

    @property
    def score_text(self) -> str:
        return self.outcome.score_text

    @property
    def answer_correct(self) -> bool:
        return self.outcome.answer_correct

    @property
    def feedback_text(self) -> str:
        return self.outcome.feedback

    @property
    def output_run_ref(self) -> str:
        final_pass = self.final_pass
        if final_pass is None:
            return ""
        return final_pass.artifacts.output_ref or final_pass.artifacts.transcript_ref

    @property
    def output_error_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.stderr_ref

    @property
    def output_system_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.system_ref

    @property
    def output_diff_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.judge_message_ref

    @property
    def team_message_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.team_message_ref

    @property
    def metadata_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.metadata_ref

    @property
    def compare_metadata_ref(self) -> str:
        final_pass = self.final_pass
        return "" if final_pass is None else final_pass.artifacts.compare_metadata_ref

    @property
    def feedback_files(self) -> tuple[str, ...]:
        final_pass = self.final_pass
        if final_pass is None or not final_pass.feedback:
            return ()
        artifacts = final_pass.artifacts
        if final_pass.runresult in {"run-error", "timelimit", "output-limit"}:
            return (artifacts.stderr_ref,) if artifacts.stderr_ref else ()
        if artifacts.judge_message_ref:
            return (artifacts.judge_message_ref,)
        if artifacts.team_message_ref:
            return (artifacts.team_message_ref,)
        return (artifacts.stderr_ref,) if artifacts.stderr_ref else ()

    def artifact_refs(self) -> set[str]:
        return set(iter_artifact_refs(self))


def iter_artifact_refs(result: ExecutionResult) -> Iterable[str]:
    for pass_result in result.passes:
        yield from pass_result.artifacts.refs()


def _usage_max(
    passes: tuple[ExecutionPassResult, ...],
    attribute: str,
) -> float | int | None:
    if not passes:
        return None
    values = [getattr(pass_result.usage, attribute) for pass_result in passes]
    if any(value is None for value in values):
        return None
    return max(cast(list[float | int], values))


def aggregate_usage(passes: Iterable[ExecutionPassResult]) -> ExecutionUsage:
    ordered = tuple(passes)
    runtime_sec = _usage_max(ordered, "runtime_sec")
    cpu_sec = _usage_max(ordered, "cpu_sec")
    wall_sec = _usage_max(ordered, "wall_sec")
    memory_kb = _usage_max(ordered, "memory_kb")
    return ExecutionUsage(
        runtime_sec=None if runtime_sec is None else float(runtime_sec),
        cpu_sec=None if cpu_sec is None else float(cpu_sec),
        wall_sec=None if wall_sec is None else float(wall_sec),
        memory_kb=None if memory_kb is None else int(memory_kb),
    )


def normalize_execution_result(
    *,
    passes: Iterable[ExecutionPassResult] = (),
    verdict: str = "",
    score_text: str = "",
    answer_correct: bool | None = None,
    error: str = "",
    feedback: str = "",
    compile_log: str = "",
    compile_diagnostics: Iterable[dict[str, object]] = (),
    warnings: Iterable[str] = (),
) -> ExecutionResult:
    ordered = tuple(sorted(passes, key=lambda item: item.number))
    if ordered:
        numbers = tuple(item.number for item in ordered)
        expected = tuple(range(1, len(ordered) + 1))
        if numbers != expected:
            raise ValueError("execution pass numbers must be contiguous and start at 1")
        for pass_result in ordered:
            if pass_result.capture_status not in CAPTURE_STATUSES:
                raise ValueError("invalid execution pass capture status")
            if pass_result.artifacts.output_ref and pass_result.artifacts.transcript_ref:
                raise ValueError("execution pass output and transcript refs are mutually exclusive")
            artifacts = pass_result.artifacts
            common_metadata = bool(
                artifacts.metadata_ref and artifacts.compare_metadata_ref
            )
            if pass_result.capture_status == CAPTURE_COMPLETE:
                if not (
                    common_metadata
                    and artifacts.input_ref
                    and (artifacts.output_ref or artifacts.transcript_ref)
                    and artifacts.stderr_ref
                    and artifacts.system_ref
                    and artifacts.judge_message_ref
                    and artifacts.team_message_ref
                ):
                    raise ValueError("complete pass capture is missing an artifact")
            elif pass_result.capture_status == CAPTURE_METADATA_INPUT_ONLY:
                if not common_metadata or not artifacts.input_ref:
                    raise ValueError("metadata-input-only pass capture is incomplete")
                if any(
                    (
                        artifacts.output_ref,
                        artifacts.transcript_ref,
                        artifacts.stderr_ref,
                        artifacts.system_ref,
                        artifacts.judge_message_ref,
                        artifacts.team_message_ref,
                    )
                ):
                    raise ValueError("metadata-input-only pass contains extra artifacts")
            elif not common_metadata or any(
                (
                    artifacts.input_ref,
                    artifacts.output_ref,
                    artifacts.transcript_ref,
                    artifacts.stderr_ref,
                    artifacts.system_ref,
                    artifacts.judge_message_ref,
                    artifacts.team_message_ref,
                )
            ):
                raise ValueError("metadata-only pass capture is invalid")
    final_pass = None if not ordered else ordered[-1]
    resolved_verdict = verdict or ("" if final_pass is None else final_pass.verdict)
    resolved_score = score_text or ("" if final_pass is None else final_pass.score_text)
    resolved_answer_correct = (
        bool(final_pass is not None and final_pass.answer_correct)
        if answer_correct is None
        else bool(answer_correct)
    )
    resolved_feedback = feedback or ("" if final_pass is None else final_pass.feedback)
    return ExecutionResult(
        outcome=ExecutionOutcome(
            verdict=resolved_verdict,
            score_text=resolved_score,
            answer_correct=resolved_answer_correct,
            usage=aggregate_usage(ordered),
            error=error,
            feedback=resolved_feedback,
        ),
        compile=CompileResult(
            log=compile_log,
            diagnostics=tuple(dict(item) for item in compile_diagnostics),
        ),
        passes=ordered,
        warnings=tuple(dict.fromkeys(token for token in warnings if token)),
    )


def execution_result_with_outcome(
    result: ExecutionResult,
    *,
    verdict: str | None = None,
    score_text: str | None = None,
    answer_correct: bool | None = None,
    error: str | None = None,
    feedback: str | None = None,
) -> ExecutionResult:
    return replace(
        result,
        outcome=replace(
            result.outcome,
            verdict=result.outcome.verdict if verdict is None else verdict,
            score_text=result.outcome.score_text if score_text is None else score_text,
            answer_correct=(
                result.outcome.answer_correct
                if answer_correct is None
                else bool(answer_correct)
            ),
            error=result.outcome.error if error is None else error,
            feedback=result.outcome.feedback if feedback is None else feedback,
        ),
    )


def _usage_dict(usage: ExecutionUsage) -> dict[str, object]:
    return {
        "runtime_sec": usage.runtime_sec,
        "cpu_sec": usage.cpu_sec,
        "wall_sec": usage.wall_sec,
        "memory_kb": usage.memory_kb,
    }


def execution_result_dict(result: ExecutionResult) -> dict[str, object]:
    return {
        "outcome": {
            "verdict": result.outcome.verdict,
            "score_text": result.outcome.score_text,
            "answer_correct": result.outcome.answer_correct,
            "usage": _usage_dict(result.outcome.usage),
            "error": result.outcome.error,
            "feedback": result.outcome.feedback,
        },
        "compile": {
            "log": result.compile.log,
            "diagnostics": [dict(item) for item in result.compile.diagnostics],
        },
        "passes": [
            {
                "number": pass_result.number,
                "capture_status": pass_result.capture_status,
                "runresult": pass_result.runresult,
                "verdict": pass_result.verdict,
                "score_text": pass_result.score_text,
                "answer_correct": pass_result.answer_correct,
                "usage": _usage_dict(pass_result.usage),
                "feedback": pass_result.feedback,
                "artifacts": {
                    "input_ref": pass_result.artifacts.input_ref,
                    "output_ref": pass_result.artifacts.output_ref,
                    "transcript_ref": pass_result.artifacts.transcript_ref,
                    "stderr_ref": pass_result.artifacts.stderr_ref,
                    "system_ref": pass_result.artifacts.system_ref,
                    "judge_message_ref": pass_result.artifacts.judge_message_ref,
                    "team_message_ref": pass_result.artifacts.team_message_ref,
                    "metadata_ref": pass_result.artifacts.metadata_ref,
                    "compare_metadata_ref": pass_result.artifacts.compare_metadata_ref,
                },
            }
            for pass_result in result.passes
        ],
        "warnings": list(result.warnings),
    }


def execution_result_json(result: ExecutionResult) -> str:
    return canonical_json(execution_result_dict(result), ensure_ascii=False)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError("execution usage value must be numeric or null")
    return max(0.0, float(cast(int | float, value)))


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("execution memory usage must be an integer or null")
    return max(0, cast(int, value))


def _usage_from_dict(raw: dict[str, object]) -> ExecutionUsage:
    return ExecutionUsage(
        runtime_sec=_optional_float(raw.get("runtime_sec")),
        cpu_sec=_optional_float(raw.get("cpu_sec")),
        wall_sec=_optional_float(raw.get("wall_sec")),
        memory_kb=_optional_int(raw.get("memory_kb")),
    )


def execution_result_from_dict(raw: dict[str, object]) -> ExecutionResult:
    if not raw:
        return ExecutionResult()
    outcome_raw = cast(dict[str, object], raw["outcome"])
    compile_raw = cast(dict[str, object], raw["compile"])
    pass_rows = cast(list[dict[str, object]], raw["passes"])
    passes: list[ExecutionPassResult] = []
    for pass_raw in pass_rows:
        pass_number = pass_raw["number"]
        if type(pass_number) is not int or cast(int, pass_number) <= 0:
            raise ValueError("execution pass number must be a positive integer")
        if type(pass_raw["answer_correct"]) is not bool:
            raise ValueError("execution pass answer_correct must be boolean")
        artifacts_raw = cast(dict[str, object], pass_raw["artifacts"])
        usage_raw = cast(dict[str, object], pass_raw["usage"])
        passes.append(
            ExecutionPassResult(
                number=cast(int, pass_number),
                capture_status=str(pass_raw["capture_status"]),
                runresult=str(pass_raw["runresult"]),
                verdict=str(pass_raw["verdict"]),
                score_text=str(pass_raw["score_text"]),
                answer_correct=cast(bool, pass_raw["answer_correct"]),
                usage=_usage_from_dict(usage_raw),
                feedback=str(pass_raw["feedback"]),
                artifacts=PassArtifacts(
                    input_ref=str(artifacts_raw["input_ref"]),
                    output_ref=str(artifacts_raw["output_ref"]),
                    transcript_ref=str(artifacts_raw["transcript_ref"]),
                    stderr_ref=str(artifacts_raw["stderr_ref"]),
                    system_ref=str(artifacts_raw["system_ref"]),
                    judge_message_ref=str(artifacts_raw["judge_message_ref"]),
                    team_message_ref=str(artifacts_raw["team_message_ref"]),
                    metadata_ref=str(artifacts_raw["metadata_ref"]),
                    compare_metadata_ref=str(artifacts_raw["compare_metadata_ref"]),
                ),
            )
        )
    if type(outcome_raw["answer_correct"]) is not bool:
        raise ValueError("execution outcome answer_correct must be boolean")
    diagnostics = cast(list[dict[str, object]], compile_raw["diagnostics"])
    warnings = cast(list[str], raw["warnings"])
    normalized = normalize_execution_result(
        passes=passes,
        verdict=str(outcome_raw["verdict"]),
        score_text=str(outcome_raw["score_text"]),
        answer_correct=cast(bool, outcome_raw["answer_correct"]),
        error=str(outcome_raw["error"]),
        feedback=str(outcome_raw["feedback"]),
        compile_log=str(compile_raw["log"]),
        compile_diagnostics=diagnostics,
        warnings=warnings,
    )
    stored_usage = _usage_from_dict(cast(dict[str, object], outcome_raw["usage"]))
    if stored_usage != normalized.outcome.usage:
        raise ValueError("stored execution outcome usage does not match pass aggregate")
    return normalized


def execution_result_from_json(text: str) -> ExecutionResult:
    raw = cast(dict[str, object], json.loads(text or "{}"))
    return execution_result_from_dict(raw)
