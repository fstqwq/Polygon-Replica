from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


_ROOT = Path(__file__).resolve().parent
_WORKLOAD_ROOT = _ROOT / "workloads"
_TASK_KINDS = ("foreground", "generate-input", "main-correct", "solution-run", "sanity")
_TOP_LEVEL_FIELDS = {
    "name",
    "seed",
    "host_count",
    "fetch_batch_size",
    "fetch_idle_backoff_sec",
    "long_poll_enabled",
    "verification_count",
    "verification_arrival_gap_sec",
    "tests_per_verification",
    "manual_test_count",
    "solution_count",
    "generator_enabled",
    "main_correct_enabled",
    "sanity_probe_count",
    "foreground_tasks",
    "cache_hit_rate",
    "compile_time_sec_by_kind",
    "case_time_distribution_by_kind",
    "slow_solution_fraction",
    "slow_solution_multiplier",
    "host_disconnect_events",
    "assertions",
}
_ASSERTION_FIELDS = {
    "max_duplicate_compile_count",
    "max_makespan_sec",
    "max_ready_to_lease_p95_sec",
    "max_unfinished_case_count",
    "max_invariant_violation_count",
    "min_host_utilization",
}


@dataclass(frozen=True)
class DurationRange:
    minimum_sec: float
    maximum_sec: float


@dataclass(frozen=True)
class HostDisconnect:
    host_index: int
    at_sec: float
    duration_sec: float


@dataclass(frozen=True)
class ForegroundTask:
    arrival_sec: float
    kind: str
    compile_time_sec: float


@dataclass(frozen=True)
class Workload:
    name: str
    seed: int
    host_count: int
    fetch_batch_size: int
    fetch_idle_backoff_sec: float
    long_poll_enabled: bool
    verification_count: int
    verification_arrival_gap_sec: float
    tests_per_verification: int
    manual_test_count: int
    solution_count: int
    generator_enabled: bool
    main_correct_enabled: bool
    sanity_probe_count: int
    foreground_tasks: tuple[ForegroundTask, ...]
    cache_hit_rate: float
    compile_time_sec_by_kind: Mapping[str, float]
    case_time_distribution_by_kind: Mapping[str, DurationRange]
    slow_solution_fraction: float
    slow_solution_multiplier: float
    host_disconnect_events: tuple[HostDisconnect, ...]
    assertions: Mapping[str, float]

    def with_overrides(
        self,
        *,
        host_count: int | None = None,
        seed: int | None = None,
    ) -> "Workload":
        return replace(
            self,
            host_count=(
                self.host_count
                if host_count is None
                else _positive_int(host_count, "host_count")
            ),
            seed=self.seed if seed is None else int(seed),
        )


def builtin_names() -> list[str]:
    return sorted(path.stem for path in _WORKLOAD_ROOT.glob("*.json"))


def load_builtin(name: str) -> Workload:
    if name not in builtin_names():
        raise ValueError(f"unknown simulation scenario: {name}")
    return load_workload(_WORKLOAD_ROOT / f"{name}.json")


def load_workload(path: Path | str) -> Workload:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workload file {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("workload root must be an object")
    return parse_workload(payload)


def parse_workload(payload: Mapping[str, object]) -> Workload:
    unknown = set(payload).difference(_TOP_LEVEL_FIELDS)
    if unknown:
        raise ValueError(f"unknown workload field: {sorted(unknown)[0]}")
    missing = _TOP_LEVEL_FIELDS.difference(payload)
    if missing:
        raise ValueError(f"missing workload field: {sorted(missing)[0]}")

    host_count = _positive_int(payload["host_count"], "host_count")
    tests_per_verification = _positive_int(
        payload["tests_per_verification"], "tests_per_verification"
    )
    manual_test_count = _nonnegative_int(payload["manual_test_count"], "manual_test_count")
    if manual_test_count > tests_per_verification:
        raise ValueError("manual_test_count exceeds tests_per_verification")

    compile_times = _float_map(payload["compile_time_sec_by_kind"], "compile_time_sec_by_kind")
    duration_payload = _mapping(
        payload["case_time_distribution_by_kind"],
        "case_time_distribution_by_kind",
    )
    durations: dict[str, DurationRange] = {}
    for kind, raw_range in duration_payload.items():
        if kind not in _TASK_KINDS:
            raise ValueError(f"unknown case duration kind: {kind}")
        range_payload = _mapping(raw_range, f"case_time_distribution_by_kind.{kind}")
        if set(range_payload) != {"min_sec", "max_sec"}:
            raise ValueError(f"invalid duration range fields for {kind}")
        minimum = _nonnegative_float(range_payload["min_sec"], f"{kind}.min_sec")
        maximum = _nonnegative_float(range_payload["max_sec"], f"{kind}.max_sec")
        if maximum < minimum:
            raise ValueError(f"duration maximum is below minimum for {kind}")
        durations[kind] = DurationRange(minimum_sec=minimum, maximum_sec=maximum)
    missing_kinds = set(_TASK_KINDS).difference(compile_times, durations)
    if missing_kinds:
        raise ValueError(f"missing timing kind: {sorted(missing_kinds)[0]}")
    unknown_compile_kinds = set(compile_times).difference(_TASK_KINDS)
    if unknown_compile_kinds:
        raise ValueError(f"unknown compile timing kind: {sorted(unknown_compile_kinds)[0]}")

    disconnects = tuple(
        _parse_disconnect(item, host_count=host_count, index=index)
        for index, item in enumerate(
            _list(payload["host_disconnect_events"], "host_disconnect_events")
        )
    )
    foreground_tasks = tuple(
        _parse_foreground_task(item, index=index)
        for index, item in enumerate(_list(payload["foreground_tasks"], "foreground_tasks"))
    )
    assertion_payload = _mapping(payload["assertions"], "assertions")
    unknown_assertions = set(assertion_payload).difference(_ASSERTION_FIELDS)
    if unknown_assertions:
        raise ValueError(f"unknown workload assertion: {sorted(unknown_assertions)[0]}")
    assertions = {
        key: _nonnegative_float(value, f"assertions.{key}")
        for key, value in assertion_payload.items()
    }

    cache_hit_rate = _fraction(payload["cache_hit_rate"], "cache_hit_rate")
    slow_solution_fraction = _fraction(
        payload["slow_solution_fraction"], "slow_solution_fraction"
    )
    slow_solution_multiplier = _nonnegative_float(
        payload["slow_solution_multiplier"], "slow_solution_multiplier"
    )
    if slow_solution_multiplier < 1.0:
        raise ValueError("slow_solution_multiplier must be at least 1")

    return Workload(
        name=_nonempty_text(payload["name"], "name"),
        seed=int(payload["seed"]),
        host_count=host_count,
        fetch_batch_size=_positive_int(payload["fetch_batch_size"], "fetch_batch_size"),
        fetch_idle_backoff_sec=_positive_float(
            payload["fetch_idle_backoff_sec"], "fetch_idle_backoff_sec"
        ),
        long_poll_enabled=_boolean(payload["long_poll_enabled"], "long_poll_enabled"),
        verification_count=_positive_int(payload["verification_count"], "verification_count"),
        verification_arrival_gap_sec=_nonnegative_float(
            payload["verification_arrival_gap_sec"], "verification_arrival_gap_sec"
        ),
        tests_per_verification=tests_per_verification,
        manual_test_count=manual_test_count,
        solution_count=_positive_int(payload["solution_count"], "solution_count"),
        generator_enabled=_boolean(payload["generator_enabled"], "generator_enabled"),
        main_correct_enabled=_boolean(payload["main_correct_enabled"], "main_correct_enabled"),
        sanity_probe_count=_nonnegative_int(payload["sanity_probe_count"], "sanity_probe_count"),
        foreground_tasks=foreground_tasks,
        cache_hit_rate=cache_hit_rate,
        compile_time_sec_by_kind=compile_times,
        case_time_distribution_by_kind=durations,
        slow_solution_fraction=slow_solution_fraction,
        slow_solution_multiplier=slow_solution_multiplier,
        host_disconnect_events=disconnects,
        assertions=assertions,
    )


def _parse_disconnect(raw: object, *, host_count: int, index: int) -> HostDisconnect:
    payload = _mapping(raw, f"host_disconnect_events[{index}]")
    if set(payload) != {"host_index", "at_sec", "duration_sec"}:
        raise ValueError(f"invalid host disconnect fields at index {index}")
    host_index = _nonnegative_int(payload["host_index"], f"disconnect[{index}].host_index")
    if host_index >= host_count:
        raise ValueError(f"host disconnect index out of range at index {index}")
    return HostDisconnect(
        host_index=host_index,
        at_sec=_nonnegative_float(payload["at_sec"], f"disconnect[{index}].at_sec"),
        duration_sec=_positive_float(payload["duration_sec"], f"disconnect[{index}].duration_sec"),
    )


def _parse_foreground_task(raw: object, *, index: int) -> ForegroundTask:
    payload = _mapping(raw, f"foreground_tasks[{index}]")
    if set(payload) != {"arrival_sec", "kind", "compile_time_sec"}:
        raise ValueError(f"invalid foreground task fields at index {index}")
    kind = _nonempty_text(payload["kind"], f"foreground_tasks[{index}].kind")
    if kind != "compile-only":
        raise ValueError(f"unsupported foreground task kind at index {index}: {kind}")
    return ForegroundTask(
        arrival_sec=_nonnegative_float(
            payload["arrival_sec"], f"foreground_tasks[{index}].arrival_sec"
        ),
        kind=kind,
        compile_time_sec=_nonnegative_float(
            payload["compile_time_sec"], f"foreground_tasks[{index}].compile_time_sec"
        ),
    )


def _mapping(raw: object, label: str) -> Mapping[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    return raw


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be an array")
    return raw


def _float_map(raw: object, label: str) -> dict[str, float]:
    payload = _mapping(raw, label)
    return {key: _nonnegative_float(value, f"{label}.{key}") for key, value in payload.items()}


def _boolean(raw: object, label: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{label} must be a boolean")
    return raw


def _nonempty_text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} must be a non-empty string")
    return raw


def _positive_int(raw: object, label: str) -> int:
    value = _nonnegative_int(raw, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _nonnegative_int(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return raw


def _positive_float(raw: object, label: str) -> float:
    value = _nonnegative_float(raw, label)
    if value <= 0.0:
        raise ValueError(f"{label} must be positive")
    return value


def _nonnegative_float(raw: object, label: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label} must be numeric")
    value = float(raw)
    if value < 0.0:
        raise ValueError(f"{label} must be non-negative")
    return value


def _fraction(raw: object, label: str) -> float:
    value = _nonnegative_float(raw, label)
    if value > 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return value
