import json
from dataclasses import dataclass
from typing import Any


SUMMARY_RUNTIME_THRESHOLD_CHECK = "summary_runtime_threshold"


@dataclass(frozen=True)
class RuntimeThresholdHit:
    source: str
    time_ms: int
    time_limit_ms: int


@dataclass(frozen=True)
class RuntimeThresholdReport:
    highlighted_tests: frozenset[str]
    warning_hit: RuntimeThresholdHit | None
    checked_count: int


def time_limit_ms_from_run_config_json(raw_json: str, *, default_ms: int = 0) -> int:
    if not raw_json:
        return _positive_int(default_ms)
    try:
        payload = json.loads(raw_json)
    except Exception:
        return _positive_int(default_ms)
    if not isinstance(payload, dict):
        return _positive_int(default_ms)
    return _positive_int(payload.get("time_limit_ms"), default=default_ms)


def evaluate_summary_runtime_threshold(
    *,
    summary: dict[str, object],
    source: str,
    time_limit_ms: int,
) -> RuntimeThresholdReport:
    if time_limit_ms <= 0:
        return RuntimeThresholdReport(highlighted_tests=frozenset(), warning_hit=None, checked_count=0)
    tests = _summary_tests(summary)
    highlighted: set[str] = set()
    checked_count = 0
    all_answer_correct = bool(tests)
    summary_time_ms = 0
    for item in tests:
        test_name = str(item.get("test") or "")
        if not test_name:
            continue
        checked_count += 1
        answer_correct = bool(item.get("answer_correct"))
        all_answer_correct = all_answer_correct and answer_correct
        time_ms = _test_time_user_ms(item)
        summary_time_ms = max(summary_time_ms, time_ms)
        if answer_correct and _time_in_threshold_band(time_ms=time_ms, time_limit_ms=time_limit_ms):
            highlighted.add(test_name)
    warning_hit = None
    if all_answer_correct and _time_in_threshold_band(time_ms=summary_time_ms, time_limit_ms=time_limit_ms):
        warning_hit = RuntimeThresholdHit(
            source=source,
            time_ms=summary_time_ms,
            time_limit_ms=time_limit_ms,
        )
    return RuntimeThresholdReport(
        highlighted_tests=frozenset(highlighted),
        warning_hit=warning_hit,
        checked_count=checked_count,
    )


def runtime_threshold_reason(hit: RuntimeThresholdHit, *, summary_has_tl: bool) -> str:
    source_label = hit.source or "solution"
    if summary_has_tl:
        return f"{source_label}: correct output in 50% extra time limit."
    return f"{source_label}: accepted solution is close to the time limit."


def _summary_tests(summary: dict[str, object]) -> list[dict[str, object]]:
    raw_tests = summary.get("tests") or []
    if not isinstance(raw_tests, list):
        return []
    return [dict(item) for item in raw_tests if isinstance(item, dict)]


def _positive_int(value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(0, parsed)


def _test_time_user_ms(item: dict[str, object]) -> int:
    time_user_ms = _positive_int(item.get("time_user_ms"))
    if time_user_ms > 0:
        return time_user_ms
    return _positive_int(item.get("time_ms"))


def _time_in_threshold_band(*, time_ms: int, time_limit_ms: int) -> bool:
    if time_ms <= 0 or time_limit_ms <= 0:
        return False
    return time_ms * 2 >= time_limit_ms and time_ms * 2 <= time_limit_ms * 3
