from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import cast

from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    CAPTURE_STATUSES,
    CompileDiagnostic,
    CompileResult,
    ExecutionOutcome,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    ExecutionWarning,
    PassArtifacts,
)


def _canonical_optional_float(value: float | int | None, *, label: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError(f"execution {label} must be numeric or null")
    result = float(cast(float | int, value))
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"execution {label} must be finite and non-negative")
    return result


def canonical_execution_usage(usage: ExecutionUsage) -> ExecutionUsage:
    if not isinstance(usage, ExecutionUsage):
        raise ValueError("execution usage must use ExecutionUsage")
    memory_kb = usage.memory_kb
    if memory_kb is not None and (type(memory_kb) is not int or memory_kb < 0):
        raise ValueError("execution memory usage must be a non-negative integer or null")
    return ExecutionUsage(
        runtime_sec=_canonical_optional_float(usage.runtime_sec, label="runtime usage"),
        cpu_sec=_canonical_optional_float(usage.cpu_sec, label="CPU usage"),
        wall_sec=_canonical_optional_float(usage.wall_sec, label="wall usage"),
        memory_kb=memory_kb,
    )


def _require_string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"execution {label} must be a string")
    return cast(str, value)


def _canonical_artifacts(artifacts: PassArtifacts) -> PassArtifacts:
    if not isinstance(artifacts, PassArtifacts):
        raise ValueError("execution pass artifacts must use PassArtifacts")
    for name, value in artifacts.__dict__.items():
        _require_string(value, label=f"pass artifact {name}")
    if artifacts.output_ref and artifacts.transcript_ref:
        raise ValueError("execution pass output and transcript refs are mutually exclusive")
    return artifacts


def _canonical_pass(pass_result: ExecutionPassResult) -> ExecutionPassResult:
    if not isinstance(pass_result, ExecutionPassResult):
        raise ValueError("execution pass must use ExecutionPassResult")
    if type(pass_result.number) is not int or pass_result.number <= 0:
        raise ValueError("execution pass number must be a positive integer")
    if pass_result.capture_status not in CAPTURE_STATUSES:
        raise ValueError("invalid execution pass capture status")
    for name in ("capture_status", "runresult", "verdict", "score_text", "feedback"):
        _require_string(getattr(pass_result, name), label=f"pass {name}")
    if type(pass_result.answer_correct) is not bool:
        raise ValueError("execution pass answer_correct must be boolean")
    artifacts = _canonical_artifacts(pass_result.artifacts)
    common_metadata = bool(artifacts.metadata_ref and artifacts.compare_metadata_ref)
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
    elif pass_result.capture_status == CAPTURE_METADATA_ONLY and (
        not common_metadata
        or any(
            (
                artifacts.input_ref,
                artifacts.output_ref,
                artifacts.transcript_ref,
                artifacts.stderr_ref,
                artifacts.system_ref,
                artifacts.judge_message_ref,
                artifacts.team_message_ref,
            )
        )
    ):
        raise ValueError("metadata-only pass capture is invalid")
    return replace(
        pass_result,
        usage=canonical_execution_usage(pass_result.usage),
        artifacts=artifacts,
    )


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


def _frozen_json_value(value: object, *, label: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError(f"{label} object keys must be strings")
        return CompileDiagnostic(
            tuple(
                (key, _frozen_json_value(item, label=f"{label}.{key}"))
                for key, item in sorted(value.items())
            )
        )
    if type(value) in {list, tuple}:
        return tuple(
            _frozen_json_value(item, label=f"{label}[{index}]")
            for index, item in enumerate(cast(Iterable[object], value))
        )
    raise ValueError(f"{label} contains a non-JSON value")


def canonical_compile_diagnostics(
    diagnostics: Iterable[Mapping[str, object]],
) -> tuple[CompileDiagnostic, ...]:
    rows: list[CompileDiagnostic] = []
    for item in diagnostics:
        if not isinstance(item, Mapping):
            raise ValueError("execution compile diagnostics must be objects")
        frozen = _frozen_json_value(item, label="execution compile diagnostic")
        if not isinstance(frozen, CompileDiagnostic):
            raise ValueError("execution compile diagnostics must be objects")
        rows.append(frozen)
    return tuple(rows)


def _canonical_warnings(
    warnings: Iterable[str | ExecutionWarning],
) -> tuple[ExecutionWarning, ...]:
    result: list[ExecutionWarning] = []
    for warning in warnings:
        token = (
            warning.message
            if isinstance(warning, ExecutionWarning)
            else _require_string(warning, label="warning")
        )
        _require_string(token, label="warning")
        if not token:
            raise ValueError("execution warning must not be empty")
        value = ExecutionWarning(message=token)
        if value not in result:
            result.append(value)
    return tuple(result)


def normalize_execution_result(
    *,
    passes: Iterable[ExecutionPassResult] = (),
    verdict: str = "",
    score_text: str = "",
    answer_correct: bool | None = None,
    error: str = "",
    feedback: str = "",
    compile_log: str = "",
    compile_diagnostics: Iterable[Mapping[str, object]] = (),
    warnings: Iterable[str | ExecutionWarning] = (),
) -> ExecutionResult:
    for label, value in (
        ("verdict", verdict),
        ("score_text", score_text),
        ("error", error),
        ("feedback", feedback),
        ("compile log", compile_log),
    ):
        _require_string(value, label=label)
    if answer_correct is not None and type(answer_correct) is not bool:
        raise ValueError("execution outcome answer_correct must be boolean or null")
    ordered = tuple(
        sorted(
            (_canonical_pass(item) for item in passes),
            key=lambda item: item.number,
        )
    )
    if ordered:
        numbers = tuple(item.number for item in ordered)
        if numbers != tuple(range(1, len(ordered) + 1)):
            raise ValueError("execution pass numbers must be contiguous and start at 1")
    final_pass = None if not ordered else ordered[-1]
    resolved_verdict = verdict or ("" if final_pass is None else final_pass.verdict)
    resolved_score = score_text or ("" if final_pass is None else final_pass.score_text)
    resolved_answer_correct = (
        bool(final_pass is not None and final_pass.answer_correct)
        if answer_correct is None
        else answer_correct
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
            diagnostics=canonical_compile_diagnostics(compile_diagnostics),
        ),
        passes=ordered,
        warnings=_canonical_warnings(warnings),
    )


def canonical_execution_result(result: ExecutionResult) -> ExecutionResult:
    if not isinstance(result, ExecutionResult):
        raise ValueError("execution result must use ExecutionResult")
    if not isinstance(result.outcome, ExecutionOutcome):
        raise ValueError("execution outcome must use ExecutionOutcome")
    if not isinstance(result.compile, CompileResult):
        raise ValueError("execution compile result must use CompileResult")
    normalized = normalize_execution_result(
        passes=result.passes,
        verdict=result.outcome.verdict,
        score_text=result.outcome.score_text,
        answer_correct=result.outcome.answer_correct,
        error=result.outcome.error,
        feedback=result.outcome.feedback,
        compile_log=result.compile.log,
        compile_diagnostics=result.compile.diagnostics,
        warnings=result.warnings,
    )
    if normalized != result:
        raise ValueError("execution result is not canonical")
    return result


def execution_result_with_outcome(
    result: ExecutionResult,
    *,
    verdict: str | None = None,
    score_text: str | None = None,
    answer_correct: bool | None = None,
    error: str | None = None,
    feedback: str | None = None,
) -> ExecutionResult:
    canonical_execution_result(result)
    values = {
        "verdict": result.outcome.verdict if verdict is None else verdict,
        "score_text": result.outcome.score_text if score_text is None else score_text,
        "error": result.outcome.error if error is None else error,
        "feedback": result.outcome.feedback if feedback is None else feedback,
    }
    for label, value in values.items():
        _require_string(value, label=label)
    resolved_answer_correct = (
        result.outcome.answer_correct if answer_correct is None else answer_correct
    )
    if type(resolved_answer_correct) is not bool:
        raise ValueError("execution outcome answer_correct must be boolean")
    return canonical_execution_result(
        replace(
            result,
            outcome=replace(
                result.outcome,
                answer_correct=resolved_answer_correct,
                **values,
            ),
        )
    )
