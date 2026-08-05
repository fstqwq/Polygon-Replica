from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from tests.scripts import simulate_judgehost
from tests.simulation.judgehost import JudgehostSimulation
from tests.simulation.workload import (
    DurationRange,
    ForegroundTask,
    HostDisconnect,
    builtin_names,
    load_builtin,
    parse_workload,
)


_KINDS = ("foreground", "generate-input", "main-correct", "solution-run", "sanity")


def _small_workload(
    *,
    case_sec: float = 3.0,
    compile_sec: float = 2.0,
    cache_hit_rate: float = 0.0,
    tests: int = 1,
    hosts: int = 1,
    disconnects: tuple[HostDisconnect, ...] = (),
    foreground_tasks: tuple[ForegroundTask, ...] = (),
):
    base = load_builtin("flat-cold")
    return replace(
        base,
        name="unit-small",
        seed=19,
        host_count=hosts,
        fetch_batch_size=2,
        long_poll_enabled=True,
        verification_count=1,
        verification_arrival_gap_sec=0.0,
        tests_per_verification=tests,
        manual_test_count=tests,
        solution_count=1,
        generator_enabled=False,
        main_correct_enabled=False,
        sanity_probe_count=0,
        foreground_tasks=foreground_tasks,
        cache_hit_rate=cache_hit_rate,
        compile_time_sec_by_kind={kind: compile_sec for kind in _KINDS},
        case_time_distribution_by_kind={
            kind: DurationRange(case_sec, case_sec) for kind in _KINDS
        },
        slow_solution_fraction=0.0,
        slow_solution_multiplier=1.0,
        host_disconnect_events=disconnects,
        assertions={
            "max_unfinished_case_count": 0.0,
            "max_invariant_violation_count": 0.0,
        },
    )


class TestJudgehostSimulation(unittest.TestCase):
    def test_builtin_workloads_are_complete_and_exclude_concurrent_identical(self) -> None:
        self.assertEqual(
            builtin_names(),
            [
                "concurrent-verifications",
                "dag-waves",
                "flat-cold",
                "host-disconnect",
                "mixed-load",
                "occur-with-foreground",
                "single-wide",
                "straggler",
                "warm-cache",
            ],
        )
        for name in builtin_names():
            with self.subTest(name=name):
                workload = load_builtin(name)
                self.assertEqual(workload.name, name)

    def test_workload_parser_rejects_unknown_and_missing_fields(self) -> None:
        source = json.loads(
            (Path(__file__).parent / "simulation" / "workloads" / "flat-cold.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaisesRegex(ValueError, "unknown workload field"):
            parse_workload({**source, "unexpected": True})
        source.pop("host_count")
        with self.assertRaisesRegex(ValueError, "missing workload field: host_count"):
            parse_workload(source)

    def test_single_host_timeline_has_exact_compile_and_case_duration(self) -> None:
        report = JudgehostSimulation(_small_workload()).run()
        summary = report["summary"]
        self.assertEqual(summary["makespan_sec"], 5.0)
        self.assertEqual(summary["compile_count"], 1)
        self.assertEqual(summary["compile_time_sec"], 2.0)
        self.assertEqual(summary["lease_attempt_count"], 1)
        self.assertEqual(summary["invariant_violation_count"], 0)

    def test_same_seed_produces_identical_report(self) -> None:
        workload = replace(
            load_builtin("dag-waves"),
            tests_per_verification=4,
            solution_count=3,
            sanity_probe_count=1,
        )
        first = JudgehostSimulation(workload, trace=True).run()
        second = JudgehostSimulation(workload, trace=True).run()
        self.assertEqual(first, second)

    def test_foreground_arrives_later_and_takes_the_next_lease(self) -> None:
        workload = _small_workload(
            case_sec=3.0,
            compile_sec=1.0,
            tests=3,
            foreground_tasks=(ForegroundTask(1.0, "compile-only", 2.0),),
        )
        report = JudgehostSimulation(workload, trace=True).run()
        trace = report["trace"]
        arrival = next(row for row in trace if row["event"] == "foreground_arrival")
        foreground_lease = next(
            row
            for row in trace
            if row["event"] == "cases_leased"
            and any(":foreground-compile-" in node for node in row["nodes"])
        )
        first_lease_after_arrival = next(
            row
            for row in trace
            if row["event"] == "cases_leased" and row["at_sec"] >= arrival["at_sec"]
        )
        last_background_report_before_foreground = max(
            row["at_sec"]
            for row in trace
            if row["event"] == "case_reported"
            and row["at_sec"] <= foreground_lease["at_sec"]
            and ":solution-" in row["node"]
        )
        self.assertEqual(arrival["at_sec"], 1.0)
        self.assertEqual(first_lease_after_arrival, foreground_lease)
        self.assertGreaterEqual(foreground_lease["at_sec"], last_background_report_before_foreground)
        self.assertEqual(report["summary"]["foreground_background_lease_count"], 0)
        self.assertTrue(report["foreground_tasks"][0]["resumed_previous_background"])

    def test_idle_host_takes_foreground_without_waiting_for_busy_host(self) -> None:
        workload = _small_workload(
            case_sec=5.0,
            compile_sec=2.0,
            tests=2,
            hosts=2,
            foreground_tasks=(ForegroundTask(1.0, "compile-only", 1.0),),
        )
        report = JudgehostSimulation(workload, trace=True).run()
        foreground_lease = next(
            row
            for row in report["trace"]
            if row["event"] == "cases_leased"
            and any(":foreground-compile-" in node for node in row["nodes"])
        )
        self.assertEqual(foreground_lease["at_sec"], 1.0)
        self.assertEqual(foreground_lease["host"], "host-2")
        self.assertEqual(report["summary"]["foreground_background_lease_count"], 0)

    def test_foreground_is_reported_separately_from_background_verification(self) -> None:
        workload = _small_workload(
            case_sec=1.0,
            compile_sec=0.5,
            foreground_tasks=(ForegroundTask(1.0, "compile-only", 2.0),),
        )
        report = JudgehostSimulation(workload).run()
        self.assertEqual(len(report["verifications"]), 1)
        self.assertEqual(len(report["foreground_tasks"]), 1)
        self.assertEqual(report["foreground_tasks"][0]["arrival_sec"], 1.0)
        self.assertGreater(report["summary"]["foreground_makespan_sec"], 0.0)
        self.assertGreater(
            report["summary"]["makespan_sec"],
            report["summary"]["background_makespan_sec"],
        )

    def test_dependencies_become_ready_only_after_parent_report(self) -> None:
        workload = replace(
            _small_workload(case_sec=1.0, compile_sec=0.5),
            generator_enabled=True,
            main_correct_enabled=True,
            manual_test_count=0,
        )
        report = JudgehostSimulation(workload, trace=True).run()
        trace = report["trace"]
        generator_report = next(
            row["at_sec"]
            for row in trace
            if row["event"] == "case_reported" and ":generator:" in row["node"]
        )
        main_ready = next(
            row["at_sec"]
            for row in trace
            if row["event"] == "task_ready" and ":main-correct:" in row["node"]
        )
        main_report = next(
            row["at_sec"]
            for row in trace
            if row["event"] == "case_reported" and ":main-correct:" in row["node"]
        )
        solution_ready = next(
            row["at_sec"]
            for row in trace
            if row["event"] == "task_ready" and ":solution-1:" in row["node"]
        )
        self.assertEqual(main_ready, generator_report)
        self.assertEqual(solution_ready, main_report)

    def test_cache_hit_never_reaches_host_execution(self) -> None:
        report = JudgehostSimulation(
            _small_workload(cache_hit_rate=1.0, tests=8, hosts=2),
            trace=True,
        ).run()
        summary = report["summary"]
        self.assertEqual(summary["cache_hit_count"], 8)
        self.assertEqual(summary["lease_attempt_count"], 0)
        self.assertEqual(summary["compile_count"], 0)
        self.assertFalse(any(row["event"] == "cases_leased" for row in report["trace"]))

    def test_disconnect_releases_case_for_a_later_lease(self) -> None:
        workload = _small_workload(
            case_sec=5.0,
            compile_sec=0.5,
            disconnects=(HostDisconnect(host_index=0, at_sec=1.0, duration_sec=1.0),),
        )
        report = JudgehostSimulation(workload, trace=True).run()
        summary = report["summary"]
        self.assertEqual(summary["lease_attempt_count"], 2)
        self.assertEqual(summary["completed_case_count"], 1)
        self.assertEqual(summary["invariant_violation_count"], 0)

    def test_ten_thousand_cache_hits_complete_without_host_payloads(self) -> None:
        report = JudgehostSimulation(
            _small_workload(cache_hit_rate=1.0, tests=10_000, hosts=16)
        ).run()
        summary = report["summary"]
        self.assertEqual(summary["case_count"], 10_000)
        self.assertEqual(summary["cache_hit_count"], 10_000)
        self.assertEqual(summary["unfinished_case_count"], 0)
        self.assertEqual(summary["invariant_violation_count"], 0)

    def test_cli_writes_report_and_trace_without_case_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            trace = Path(tmp) / "trace.json"
            exit_code = simulate_judgehost.main(
                [
                    "--scenario",
                    "single-wide",
                    "--hosts",
                    "2",
                    "--seed",
                    "31",
                    "--trace",
                    str(trace),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            report_payload = json.loads(output.read_text(encoding="utf-8"))
            trace_text = trace.read_text(encoding="utf-8")
            self.assertEqual(report_payload["seeds"], [31])
            self.assertNotIn("input_ref", trace_text)
            self.assertNotIn("answer_ref", trace_text)

    def test_cli_compares_all_scheduler_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "strategies.json"
            exit_code = simulate_judgehost.main(
                [
                    "--scenario",
                    "occur-with-foreground",
                    "--repeat",
                    "2",
                    "--all-strategies",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload["strategies"]),
                {
                    "production",
                    "legacy-production",
                    "naive-no-affinity",
                    "affinity-foreground-last",
                    "affinity-foreground-first",
                    "dispatch-scope-pending-parallel",
                },
            )

    def test_cli_ablates_selection_stage_presence_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "stage-ablation.json"
            with redirect_stdout(io.StringIO()):
                exit_code = simulate_judgehost.main(
                    [
                        "--scenario",
                        "dag-waves",
                        "--repeat",
                        "1",
                        "--selection-stage-ablation",
                        "--parallel-compile",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["parallel_compile"])
            self.assertEqual(
                set(payload["presence"]),
                {
                    "all",
                    "without-affinity",
                    "without-stolen",
                    "without-prerequisite",
                    "without-undispatched",
                },
            )
            self.assertEqual(len(payload["orders"]), 24)
            self.assertEqual(payload["assertion_failures"], [])
            for row in (*payload["presence"].values(), *payload["orders"].values()):
                self.assertEqual(
                    row["runs"][0]["summary"]["invariant_violation_count"],
                    0,
                )

    def test_cli_compares_exact_observed_and_oracle_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "score-strategies.json"
            with redirect_stdout(io.StringIO()):
                exit_code = simulate_judgehost.main(
                    [
                        "--scenario",
                        "dag-waves",
                        "--repeat",
                        "1",
                        "--score-strategies",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload["strategies"]),
                {
                    f"{mode}/{score}"
                    for mode in ("spread-first", "score-all")
                    for score in (
                        "pending-count",
                        "pending-per-active-host",
                        "observed-work",
                        "observed-work-per-active-host",
                        "observed-marginal-saving",
                        "oracle-marginal-saving",
                    )
                },
            )
            for row in payload["strategies"].values():
                self.assertEqual(
                    row["runs"][0]["summary"]["invariant_violation_count"],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
