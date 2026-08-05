from __future__ import annotations

import statistics
from typing import Mapping


_NUMERIC_FIELDS = (
    "makespan_sec",
    "background_makespan_sec",
    "foreground_ready_to_lease_sec",
    "foreground_ready_to_compile_start_sec",
    "foreground_completion_sec",
    "foreground_makespan_sec",
    "foreground_wait_for_current_lease_sec",
    "foreground_background_lease_count",
    "foreground_resume_count",
    "foreground_resume_same_batch_count",
    "duplicate_compile_count",
    "compile_count",
    "compile_time_sec",
    "cache_hit_count",
    "leased_case_count",
    "duplicate_lease_count",
    "duplicate_report_count",
    "ready_to_lease_p95_sec",
    "average_host_utilization",
    "program_switch_count",
    "empty_fetch_count",
    "poll_backoff_sec",
    "long_poll_wake_count",
    "invariant_violation_count",
    "unfinished_case_count",
    "dangling_batch_count",
)


def aggregate_reports(reports: list[dict[str, object]]) -> dict[str, object]:
    if not reports:
        raise ValueError("at least one simulation report is required")
    aggregates = {}
    for metric_name in _NUMERIC_FIELDS:
        values = [float(_summary(report)[metric_name]) for report in reports]
        aggregates[metric_name] = {
            "median": _round(statistics.median(values)),
            "p95": _round(_percentile(values, 0.95)),
            "minimum": _round(min(values)),
            "maximum": _round(max(values)),
        }
    return {
        "schema_version": 2,
        "workload": str(reports[0]["workload"]),
        "run_count": len(reports),
        "aggregate": aggregates,
    }


def compare_aggregates(
    current: Mapping[str, object],
    baseline: Mapping[str, object],
) -> dict[str, object]:
    current_metrics = _mapping_field(current, "aggregate")
    baseline_metrics = _mapping_field(baseline, "aggregate")
    deltas: dict[str, object] = {}
    for metric_name in sorted(set(current_metrics).intersection(baseline_metrics)):
        current_row = _mapping_field(current_metrics, metric_name)
        baseline_row = _mapping_field(baseline_metrics, metric_name)
        current_value = float(current_row["median"])
        baseline_value = float(baseline_row["median"])
        deltas[metric_name] = {
            "baseline": _round(baseline_value),
            "current": _round(current_value),
            "delta": _round(current_value - baseline_value),
            "relative_delta": None
            if baseline_value == 0.0
            else _round((current_value - baseline_value) / baseline_value),
        }
    return {
        "baseline_workload": baseline.get("workload"),
        "current_workload": current.get("workload"),
        "metrics": deltas,
    }


def evaluate_assertions(
    report: Mapping[str, object], assertions: Mapping[str, float]
) -> list[str]:
    summary = _summary(report)
    checks = {
        "max_duplicate_compile_count": (
            "duplicate_compile_count",
            lambda actual, limit: actual <= limit,
        ),
        "max_makespan_sec": ("makespan_sec", lambda actual, limit: actual <= limit),
        "max_ready_to_lease_p95_sec": (
            "ready_to_lease_p95_sec",
            lambda actual, limit: actual <= limit,
        ),
        "max_unfinished_case_count": (
            "unfinished_case_count",
            lambda actual, limit: actual <= limit,
        ),
        "max_invariant_violation_count": (
            "invariant_violation_count",
            lambda actual, limit: actual <= limit,
        ),
        "min_host_utilization": (
            "average_host_utilization",
            lambda actual, limit: actual >= limit,
        ),
    }
    failures = []
    for assertion, limit in assertions.items():
        field, predicate = checks[assertion]
        actual = float(summary[field])
        if not predicate(actual, float(limit)):
            failures.append(f"{assertion}: actual={actual:g} limit={float(limit):g}")
    return failures


def _summary(report: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping_field(report, "summary")


def _mapping_field(payload: Mapping[str, object], field: str) -> Mapping[str, object]:
    value = payload[field]
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _round(value: float) -> float:
    return round(float(value), 6)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
