import json
import math
from collections.abc import Iterable
from typing import cast

from app.service.execution.model import (
    CompileResult,
    CompileDiagnostic,
    ExecutionOutcome,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    ExecutionWarning,
    PassArtifacts,
)
from app.service.execution.policy import (
    canonical_compile_diagnostics,
    canonical_execution_result,
)
from app.service.platform.hashing import canonical_json


_RESULT_KEYS = frozenset(("outcome", "compile", "passes", "warnings"))
_OUTCOME_KEYS = frozenset(
    ("verdict", "score_text", "answer_correct", "usage", "error", "feedback")
)
_COMPILE_KEYS = frozenset(("log", "diagnostics"))
_USAGE_KEYS = frozenset(("runtime_sec", "cpu_sec", "wall_sec", "memory_kb"))
_PASS_KEYS = frozenset(
    (
        "number",
        "capture_status",
        "runresult",
        "verdict",
        "score_text",
        "answer_correct",
        "usage",
        "feedback",
        "artifacts",
    )
)
_ARTIFACT_KEYS = frozenset(
    (
        "input_ref",
        "output_ref",
        "transcript_ref",
        "stderr_ref",
        "system_ref",
        "judge_message_ref",
        "team_message_ref",
        "metadata_ref",
        "compare_metadata_ref",
    )
)


def _object(value: object, *, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw):
        raise ValueError(f"{label} keys must be strings")
    return cast(dict[str, object], raw)


def _array(value: object, *, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _exact_keys(raw: dict[str, object], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        unsupported = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unsupported:
            details.append("unsupported " + ", ".join(unsupported))
        raise ValueError(f"{label} has invalid fields: {'; '.join(details)}")


def _string(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    return cast(str, value)


def _boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean")
    return cast(bool, value)


def _optional_float(value: object, *, label: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be numeric or null")
    result = float(cast(int | float, value))
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or cast(int, value) < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return cast(int, value)


def _usage_from_object(value: object, *, label: str) -> ExecutionUsage:
    raw = _object(value, label=label)
    _exact_keys(raw, _USAGE_KEYS, label=label)
    return ExecutionUsage(
        runtime_sec=_optional_float(raw["runtime_sec"], label=f"{label}.runtime_sec"),
        cpu_sec=_optional_float(raw["cpu_sec"], label=f"{label}.cpu_sec"),
        wall_sec=_optional_float(raw["wall_sec"], label=f"{label}.wall_sec"),
        memory_kb=_optional_int(raw["memory_kb"], label=f"{label}.memory_kb"),
    )


def _usage_dict(usage: ExecutionUsage) -> dict[str, object]:
    return {
        "runtime_sec": usage.runtime_sec,
        "cpu_sec": usage.cpu_sec,
        "wall_sec": usage.wall_sec,
        "memory_kb": usage.memory_kb,
    }


def _json_value(value: object, *, label: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(cast(float, value)):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if type(value) in {list, tuple}:
        return [
            _json_value(item, label=f"{label}[{index}]")
            for index, item in enumerate(cast(list[object] | tuple[object, ...], value))
        ]
    if isinstance(value, CompileDiagnostic):
        return {
            key: _json_value(item, label=f"{label}.{key}")
            for key, item in value.items()
        }
    if type(value) is dict:
        raw = _object(value, label=label)
        return {
            key: _json_value(item, label=f"{label}.{key}")
            for key, item in raw.items()
        }
    raise ValueError(f"{label} contains a non-JSON value")


def compile_diagnostics_payload(
    diagnostics: Iterable[CompileDiagnostic],
) -> list[dict[str, object]]:
    canonical = canonical_compile_diagnostics(diagnostics)
    return [
        cast(
            dict[str, object],
            _json_value(item, label="execution compile diagnostic"),
        )
        for item in canonical
    ]


def execution_result_dict(result: ExecutionResult) -> dict[str, object]:
    canonical_execution_result(result)
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
            "diagnostics": compile_diagnostics_payload(
                result.compile.diagnostics
            ),
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
                    name: getattr(pass_result.artifacts, name)
                    for name in sorted(_ARTIFACT_KEYS)
                },
            }
            for pass_result in result.passes
        ],
        "warnings": [warning.message for warning in result.warnings],
    }


def execution_result_json(result: ExecutionResult) -> str:
    return canonical_json(execution_result_dict(result), ensure_ascii=False)


def _pass_from_object(value: object, *, index: int) -> ExecutionPassResult:
    label = f"execution pass {index}"
    raw = _object(value, label=label)
    _exact_keys(raw, _PASS_KEYS, label=label)
    number = raw["number"]
    if type(number) is not int or cast(int, number) <= 0:
        raise ValueError(f"{label}.number must be a positive integer")
    artifacts_raw = _object(raw["artifacts"], label=f"{label}.artifacts")
    _exact_keys(artifacts_raw, _ARTIFACT_KEYS, label=f"{label}.artifacts")
    return ExecutionPassResult(
        number=cast(int, number),
        capture_status=_string(raw["capture_status"], label=f"{label}.capture_status"),
        runresult=_string(raw["runresult"], label=f"{label}.runresult"),
        verdict=_string(raw["verdict"], label=f"{label}.verdict"),
        score_text=_string(raw["score_text"], label=f"{label}.score_text"),
        answer_correct=_boolean(
            raw["answer_correct"],
            label=f"{label}.answer_correct",
        ),
        usage=_usage_from_object(raw["usage"], label=f"{label}.usage"),
        feedback=_string(raw["feedback"], label=f"{label}.feedback"),
        artifacts=PassArtifacts(
            **{
                name: _string(artifacts_raw[name], label=f"{label}.artifacts.{name}")
                for name in _ARTIFACT_KEYS
            }
        ),
    )


def execution_result_from_dict(value: object) -> ExecutionResult:
    raw = _object(value, label="execution result")
    _exact_keys(raw, _RESULT_KEYS, label="execution result")
    outcome_raw = _object(raw["outcome"], label="execution outcome")
    _exact_keys(outcome_raw, _OUTCOME_KEYS, label="execution outcome")
    compile_raw = _object(raw["compile"], label="execution compile")
    _exact_keys(compile_raw, _COMPILE_KEYS, label="execution compile")
    pass_values = _array(raw["passes"], label="execution passes")
    passes = tuple(
        _pass_from_object(value, index=index)
        for index, value in enumerate(pass_values, start=1)
    )
    if tuple(item.number for item in passes) != tuple(range(1, len(passes) + 1)):
        raise ValueError("execution pass numbers must be ordered, contiguous, and start at 1")
    diagnostic_values = _array(
        compile_raw["diagnostics"],
        label="execution compile diagnostics",
    )
    diagnostics = canonical_compile_diagnostics(
        _object(value, label=f"execution compile diagnostic {index}")
        for index, value in enumerate(diagnostic_values, start=1)
    )
    warning_values = _array(raw["warnings"], label="execution warnings")
    warnings = tuple(
        ExecutionWarning(
            message=_string(value, label=f"execution warning {index}")
        )
        for index, value in enumerate(warning_values, start=1)
    )
    result = ExecutionResult(
        outcome=ExecutionOutcome(
            verdict=_string(outcome_raw["verdict"], label="execution outcome.verdict"),
            score_text=_string(
                outcome_raw["score_text"],
                label="execution outcome.score_text",
            ),
            answer_correct=_boolean(
                outcome_raw["answer_correct"],
                label="execution outcome.answer_correct",
            ),
            usage=_usage_from_object(
                outcome_raw["usage"],
                label="execution outcome.usage",
            ),
            error=_string(outcome_raw["error"], label="execution outcome.error"),
            feedback=_string(
                outcome_raw["feedback"],
                label="execution outcome.feedback",
            ),
        ),
        compile=CompileResult(
            log=_string(compile_raw["log"], label="execution compile.log"),
            diagnostics=diagnostics,
        ),
        passes=passes,
        warnings=warnings,
    )
    return canonical_execution_result(result)


def execution_result_from_json(text: str) -> ExecutionResult:
    if type(text) is not str:
        raise ValueError("execution result JSON must be text")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("execution result JSON is malformed") from exc
    return execution_result_from_dict(raw)
