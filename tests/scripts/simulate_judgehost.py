#!/usr/bin/env python3
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
from tests.simulation.strategy import (
    SCORE_STRATEGY_NAMES,
    STRATEGY_NAMES,
    selection_ablation_strategies,
    selection_strategy_name,
)
from tests.simulation.workload import Workload, builtin_names, load_builtin, load_workload


_SCORE_MODES = (
    ("spread-first", "score-parallel:"),
    ("score-all", "score-global-parallel:"),
)


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
    parser.add_argument("--strategy", choices=STRATEGY_NAMES, default="production")
    parser.add_argument("--all-strategies", action="store_true")
    parser.add_argument("--selection-stage-ablation", action="store_true")
    parser.add_argument("--parallel-compile", action="store_true")
    parser.add_argument("--score-strategies", action="store_true")
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
    if "foreground_makespan_sec" in aggregate:
        print(
            "background median={background:.6f}s foreground median={foreground:.6f}s "
            "foreground p95={foreground_p95:.6f}s".format(
                background=aggregate["background_makespan_sec"]["median"],
                foreground=aggregate["foreground_makespan_sec"]["median"],
                foreground_p95=aggregate["foreground_makespan_sec"]["p95"],
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


def _print_strategy_table(payloads: dict[str, dict[str, Any]]) -> None:
    print(
        "| Strategy | Background median/p95/worst | Foreground median/p95/worst | "
        "Background leases while waiting (worst) | Compile median | Switches median |"
    )
    print("|---|---:|---:|---:|---:|---:|")
    for strategy in STRATEGY_NAMES:
        aggregate = payloads[strategy]["aggregate"]["aggregate"]
        background = aggregate["background_makespan_sec"]
        foreground = aggregate["foreground_makespan_sec"]
        print(
            "| {strategy} | {bg_median:.3f}/{bg_p95:.3f}/{bg_worst:.3f}s | "
            "{fg_median:.3f}/{fg_p95:.3f}/{fg_worst:.3f}s | {leases:.0f} | "
            "{compile_count:.1f} | {switches:.1f} |".format(
                strategy=strategy,
                bg_median=background["median"],
                bg_p95=background["p95"],
                bg_worst=background["maximum"],
                fg_median=foreground["median"],
                fg_p95=foreground["p95"],
                fg_worst=foreground["maximum"],
                leases=aggregate["foreground_background_lease_count"]["maximum"],
                compile_count=aggregate["compile_count"]["median"],
                switches=aggregate["program_switch_count"]["median"],
            )
        )


def _metric_median(payload: dict[str, Any], metric: str) -> float:
    return float(payload["aggregate"]["aggregate"][metric]["median"])


def _relative_percent(current: float, baseline: float) -> float:
    return 0.0 if baseline == 0.0 else 100.0 * (current - baseline) / baseline


def _print_selection_stage_ablation(payload: dict[str, Any]) -> None:
    baseline = payload["baseline"]
    baseline_makespan = _metric_median(baseline, "background_makespan_sec")
    print("\nPresence ablation")
    print("| Policy | Background median | Delta | Foreground median | Compile median | Switches median |")
    print("|---|---:|---:|---:|---:|---:|")
    for label, row in payload["presence"].items():
        makespan = _metric_median(row, "background_makespan_sec")
        print(
            "| {label} | {makespan:.3f}s | {delta:+.2f}% | {foreground:.3f}s | "
            "{compiles:.1f} | {switches:.1f} |".format(
                label=label,
                makespan=makespan,
                delta=_relative_percent(makespan, baseline_makespan),
                foreground=_metric_median(row, "foreground_makespan_sec"),
                compiles=_metric_median(row, "compile_count"),
                switches=_metric_median(row, "program_switch_count"),
            )
        )

    print("\nOrder ablation (sorted by background median)")
    print("| Rank | Order | Background median | Delta | Foreground median | Compile median | Switches median |")
    print("|---:|---|---:|---:|---:|---:|---:|")
    ranked = sorted(
        payload["orders"].items(),
        key=lambda item: (_metric_median(item[1], "background_makespan_sec"), item[0]),
    )
    for rank, (label, row) in enumerate(ranked, start=1):
        makespan = _metric_median(row, "background_makespan_sec")
        print(
            "| {rank} | {label} | {makespan:.3f}s | {delta:+.2f}% | {foreground:.3f}s | "
            "{compiles:.1f} | {switches:.1f} |".format(
                rank=rank,
                label=label,
                makespan=makespan,
                delta=_relative_percent(makespan, baseline_makespan),
                foreground=_metric_median(row, "foreground_makespan_sec"),
                compiles=_metric_median(row, "compile_count"),
                switches=_metric_median(row, "program_switch_count"),
            )
        )


def _print_score_strategy_table(payloads: dict[str, dict[str, Any]]) -> None:
    baseline = payloads["spread-first/pending-count"]
    baseline_makespan = _metric_median(baseline, "background_makespan_sec")
    print(
        "| Score | Background median | Delta | Foreground median | "
        "Ready-to-lease p95 | Compile median | Switches median |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|")
    for mode, _prefix in _SCORE_MODES:
        for score_name in SCORE_STRATEGY_NAMES:
            label = f"{mode}/{score_name}"
            row = payloads[label]
            makespan = _metric_median(row, "background_makespan_sec")
            print(
                "| {score} | {makespan:.3f}s | {delta:+.2f}% | {foreground:.3f}s | "
                "{ready:.3f}s | {compiles:.1f} | {switches:.1f} |".format(
                    score=label,
                    makespan=makespan,
                    delta=_relative_percent(makespan, baseline_makespan),
                    foreground=_metric_median(row, "foreground_makespan_sec"),
                    ready=_metric_median(row, "ready_to_lease_p95_sec"),
                    compiles=_metric_median(row, "compile_count"),
                    switches=_metric_median(row, "program_switch_count"),
                )
            )

def _run_strategy(
    workload: Workload,
    *,
    strategy: str,
    repeat: int,
    trace_enabled: bool,
) -> tuple[dict[str, Any], list[str]]:
    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    for index in range(repeat):
        run_workload = workload.with_overrides(seed=workload.seed + index)
        report = JudgehostSimulation(
            run_workload,
            trace=trace_enabled,
            strategy=strategy,
        ).run()
        failures.extend(
            f"{strategy} run {index}: {message}"
            for message in evaluate_assertions(report, run_workload.assertions)
        )
        reports.append(report)
    return (
        {
            "scenario": workload.name,
            "strategy": strategy,
            "host_count": workload.host_count,
            "seeds": [report["seed"] for report in reports],
            "runs": reports,
            "aggregate": aggregate_reports(reports),
            "assertion_failures": failures,
        },
        failures,
    )


def _run_selection_stage_ablation(
    workload: Workload,
    *,
    repeat: int,
    parallel_compile: bool,
) -> tuple[dict[str, Any], list[str]]:
    _, baseline_stages, omissions, orders = selection_ablation_strategies()
    baseline_name = selection_strategy_name(
        baseline_stages,
        parallel_compile=parallel_compile,
    )
    stage_sets = {baseline_stages, *omissions.values(), *orders}
    results: dict[tuple[str, ...], dict[str, Any]] = {}
    failures: list[str] = []
    for stages in sorted(stage_sets):
        result, result_failures = _run_strategy(
            workload,
            strategy=selection_strategy_name(
                stages,
                parallel_compile=parallel_compile,
            ),
            repeat=repeat,
            trace_enabled=False,
        )
        results[stages] = result
        failures.extend(result_failures)
    return (
        {
            "scenario": workload.name,
            "host_count": workload.host_count,
            "repeat": repeat,
            "parallel_compile": parallel_compile,
            "stage_labels": {
                "affinity": "reuse a ready Batch in the host affinity queue",
                "stolen": "continue the host's single stolen Batch",
                "prerequisite": "help a prerequisite of a blocked affinity Batch",
                "undispatched": "prefer a Batch that has never been dispatched",
            },
            "fallback": "largest pending Case count, then scope_sequence and batch_id",
            "baseline_strategy": baseline_name,
            "baseline": results[baseline_stages],
            "presence": {
                "all": results[baseline_stages],
                **{label: results[stages] for label, stages in omissions.items()},
            },
            "orders": {
                " > ".join(stages): results[stages]
                for stages in orders
            },
            "assertion_failures": failures,
        },
        failures,
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
    if args.trace is not None and args.all_strategies:
        parser.error("--trace cannot be combined with --all-strategies")
    if args.compare is not None and args.all_strategies:
        parser.error("--compare cannot be combined with --all-strategies")
    if args.selection_stage_ablation and args.all_strategies:
        parser.error("--selection-stage-ablation cannot be combined with --all-strategies")
    if args.selection_stage_ablation and args.trace is not None:
        parser.error("--selection-stage-ablation cannot be combined with --trace")
    if args.selection_stage_ablation and args.compare is not None:
        parser.error("--selection-stage-ablation cannot be combined with --compare")
    if args.parallel_compile and not args.selection_stage_ablation:
        parser.error("--parallel-compile requires --selection-stage-ablation")
    if args.score_strategies and args.all_strategies:
        parser.error("--score-strategies cannot be combined with --all-strategies")
    if args.score_strategies and args.selection_stage_ablation:
        parser.error("--score-strategies cannot be combined with stage ablation")
    if args.score_strategies and (args.trace is not None or args.compare is not None):
        parser.error("--score-strategies cannot be combined with --trace or --compare")

    try:
        workload = (
            load_workload(args.workload)
            if args.workload
            else load_builtin(args.scenario or "dag-waves")
        )
        workload = workload.with_overrides(host_count=args.hosts, seed=args.seed)
        if args.score_strategies:
            strategy_payloads: dict[str, dict[str, Any]] = {}
            failures: list[str] = []
            for mode, prefix in _SCORE_MODES:
                for score_name in SCORE_STRATEGY_NAMES:
                    strategy_payload, strategy_failures = _run_strategy(
                        workload,
                        strategy=f"{prefix}{score_name}",
                        repeat=args.repeat,
                        trace_enabled=False,
                    )
                    strategy_payloads[f"{mode}/{score_name}"] = strategy_payload
                    failures.extend(strategy_failures)
            payload = {
                "scenario": workload.name,
                "host_count": workload.host_count,
                "parallel_compile": True,
                "strategies": strategy_payloads,
                "assertion_failures": failures,
            }
            if args.output is not None:
                _write_json(args.output, payload)
            _print_score_strategy_table(strategy_payloads)
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1 if failures else 0
        if args.selection_stage_ablation:
            payload, failures = _run_selection_stage_ablation(
                workload,
                repeat=args.repeat,
                parallel_compile=args.parallel_compile,
            )
            if args.output is not None:
                _write_json(args.output, payload)
            _print_selection_stage_ablation(payload)
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1 if failures else 0
        if args.all_strategies:
            strategy_payloads: dict[str, dict[str, Any]] = {}
            failures: list[str] = []
            for strategy in STRATEGY_NAMES:
                strategy_payload, strategy_failures = _run_strategy(
                    workload,
                    strategy=strategy,
                    repeat=args.repeat,
                    trace_enabled=False,
                )
                strategy_payloads[strategy] = strategy_payload
                failures.extend(strategy_failures)
            payload = {
                "scenario": workload.name,
                "host_count": workload.host_count,
                "strategies": strategy_payloads,
                "assertion_failures": failures,
            }
            if args.output is not None:
                _write_json(args.output, payload)
            _print_strategy_table(strategy_payloads)
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1 if failures else 0

        payload, failures = _run_strategy(
            workload,
            strategy=args.strategy,
            repeat=args.repeat,
            trace_enabled=args.trace is not None,
        )
        if args.compare is not None:
            payload["comparison"] = compare_aggregates(
                payload["aggregate"], _load_baseline(args.compare)
            )
        if args.trace is not None:
            report = payload["runs"][0]
            _write_json(
                args.trace,
                {
                    "scenario": workload.name,
                    "seed": report["seed"],
                    "strategy": args.strategy,
                    "trace": report.pop("trace"),
                },
            )
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
