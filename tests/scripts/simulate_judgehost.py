#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tests.simulation.judgehost import JudgehostSimulation
from tests.simulation.report import (
    aggregate_reports,
    compare_aggregates,
    evaluate_assertions,
)
from tests.simulation.workload import builtin_names, load_builtin, load_workload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic judgehost scheduling simulations."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--scenario", choices=builtin_names())
    source.add_argument("--workload", type=Path)
    parser.add_argument("--hosts", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--list", action="store_true", dest="list_scenarios")
    return parser


def _load_baseline(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline must be a JSON object")
    if "run_count" in payload and "aggregate" in payload:
        return payload
    if "aggregate" in payload:
        aggregate_report = payload["aggregate"]
        if not isinstance(aggregate_report, dict):
            raise ValueError("baseline aggregate must be a JSON object")
        return aggregate_report
    if "runs" in payload:
        runs = payload["runs"]
        if not isinstance(runs, list) or not runs:
            raise ValueError("baseline runs must be a non-empty JSON array")
        return aggregate_reports(runs)
    if "makespan_sec" in payload:
        return payload
    raise ValueError("baseline does not contain aggregate, runs, or report metrics")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_summary(payload: dict[str, Any]) -> None:
    aggregate_report = payload["aggregate"]
    aggregate = aggregate_report["aggregate"]
    print(
        "scenario={scenario} repeats={repeat_count} hosts={hosts}".format(
            scenario=payload["scenario"],
            repeat_count=aggregate_report["run_count"],
            hosts=payload["host_count"],
        )
    )
    print(
        "makespan median={median:.6f}s min={minimum:.6f}s max={maximum:.6f}s".format(
            median=aggregate["makespan_sec"]["median"],
            minimum=aggregate["makespan_sec"]["minimum"],
            maximum=aggregate["makespan_sec"]["maximum"],
        )
    )
    print(
        "compile median={compile_count:.1f} duplicate median={duplicates:.1f} "
        "utilization median={utilization:.3f}".format(
            compile_count=aggregate["compile_count"]["median"],
            duplicates=aggregate["duplicate_compile_count"]["median"],
            utilization=aggregate["average_host_utilization"]["median"],
        )
    )
    print(
        "cases leased median={leased:.1f} cache-hit median={hits:.1f} "
        "unfinished max={unfinished:.0f}".format(
            leased=aggregate["leased_case_count"]["median"],
            hits=aggregate["cache_hit_count"]["median"],
            unfinished=aggregate["unfinished_case_count"]["maximum"],
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list_scenarios:
        print("\n".join(builtin_names()))
        return 0
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.trace is not None and args.repeat != 1:
        parser.error("--trace requires --repeat 1")

    try:
        workload = (
            load_workload(args.workload)
            if args.workload
            else load_builtin(args.scenario or "dag-waves")
        )
        workload = workload.with_overrides(host_count=args.hosts, seed=args.seed)
        reports: list[dict[str, Any]] = []
        trace_payload: dict[str, Any] | None = None
        failures: list[str] = []
        for index in range(args.repeat):
            run_workload = workload.with_overrides(seed=workload.seed + index)
            report = JudgehostSimulation(run_workload, trace=args.trace is not None).run()
            failures.extend(
                f"run {index}: {message}"
                for message in evaluate_assertions(report, run_workload.assertions)
            )
            if args.trace is not None:
                trace_payload = {
                    "scenario": run_workload.name,
                    "seed": run_workload.seed,
                    "trace": report.pop("trace"),
                }
            reports.append(report)
        aggregate = aggregate_reports(reports)
        payload: dict[str, Any] = {
            "scenario": workload.name,
            "host_count": workload.host_count,
            "seeds": [report["seed"] for report in reports],
            "runs": reports,
            "aggregate": aggregate,
            "assertion_failures": failures,
        }
        if args.compare is not None:
            payload["comparison"] = compare_aggregates(aggregate, _load_baseline(args.compare))
        if args.trace is not None and trace_payload is not None:
            _write_json(args.trace, trace_payload)
        if args.output is not None:
            _write_json(args.output, payload)
        _print_summary(payload)
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1 if failures else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"simulation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
