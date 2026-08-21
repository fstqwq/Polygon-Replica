import json

from app.service.verification.diagnostic import (
    TaskDiagnosticSnapshot,
    merge_task_diagnostic_snapshot,
    new_task_diagnostic_item,
    task_diagnostic_snapshot_json,
)
from app.service.verification.lifecycle import verification_task_id
from app.service.verification.task_metadata import (
    canonical_diagnostics,
    canonical_truncated_text,
    diagnostics_json_text,
)
from app.service.verification.types import VerificationStatus, VerificationTaskStatus

from tests.identity_helpers import canonical_test_verification_id
from tests.verification_policy_fixture import VerificationPolicyTestBase


class TestVerificationPolicy(VerificationPolicyTestBase):
    def test_memory_limit_is_canonical_before_verification_payloads(self) -> None:
        from app.service.verification.execution_plan import (
            _generate_payload_base,
            _problem_limits,
            _run_payload_base,
        )
        from app.service.problem.runtime_config import (
            ProblemConfig,
            ProblemConfigLimits,
            parse_problem_config,
        )

        config_limits = ProblemConfigLimits(100, 30000, 1, 2048, 1, 64)
        runtime = ProblemConfig(
            time_limit_ms=2000,
            memory_limit_mb=1,
            mode="pass-fail",
            pass_limit=1,
        )
        self.assertEqual(
            parse_problem_config(
                json.dumps(runtime),
                limits=config_limits,
            )["memory_limit_mb"],
            1,
        )
        invalid_configs = (
            {**runtime, "memory_limit_mb": 0},
            {**runtime, "memory_limit_mb": "invalid"},
            {key: value for key, value in runtime.items() if key != "memory_limit_mb"},
            {**runtime, "memory_limit_mb": 4096},
        )
        for invalid in invalid_configs:
            with self.subTest(problem_config=invalid), self.assertRaises(ValueError):
                parse_problem_config(json.dumps(invalid), limits=config_limits)

        limits = _problem_limits(runtime)
        run_payload = _run_payload_base(
            problem_mode="pass-fail",
            problem_limits=limits,
            source_files={},
        )
        generate_payload = _generate_payload_base(
            problem_mode="pass-fail",
            problem_limits=limits,
            source_files={},
        )
        self.assertEqual(limits["memory_limit_mb"], 1)
        self.assertEqual(
            json.loads(str(run_payload["run_config_json"]))["memory_limit_mb"],
            1,
        )
        self.assertEqual(
            json.loads(str(generate_payload["run_config_json"]))["memory_limit_mb"],
            1,
        )

    def test_effective_verification_status_waits_for_pending_sanity_checks(self) -> None:
        from app.service.verification.sanity import effective_verification_status

        counts = {
            "pending": 0,
            "queued": 0,
            "running": 0,
        }
        status, finished = effective_verification_status(
            task_status="ok",
            counts=counts,
            sanity_checks=["custom_sample_output"],
            sanity_status="pending",
        )
        self.assertEqual(status, "running")
        self.assertFalse(finished)

        status, finished = effective_verification_status(
            task_status="ok",
            counts=counts,
            sanity_checks=["custom_sample_output"],
            sanity_status="passed",
        )
        self.assertEqual(status, "ok")
        self.assertTrue(finished)

        status, finished = effective_verification_status(
            task_status="ok",
            counts=counts,
            sanity_checks=["custom_sample_output"],
            sanity_status="failed",
        )
        self.assertEqual(status, "ok")
        self.assertTrue(finished)

        status, finished = effective_verification_status(
            task_status="ok",
            counts=counts,
            sanity_checks=["boundary_coverage"],
            sanity_status="warning",
        )
        self.assertEqual(status, "ok")
        self.assertTrue(finished)

    def test_planned_sanity_checks_include_stability_probes(self) -> None:
        from app.service.verification.sanity import (
            BOUNDARY_COVERAGE_CHECK,
            CUSTOM_SAMPLE_OUTPUT_CHECK,
            EMPTY_OUTPUT_STABILITY_CHECK,
            SUMMARY_RUNTIME_THRESHOLD_CHECK,
            UNICODE_OUTPUT_STABILITY_CHECK,
            planned_sanity_checks,
        )

        self.assertEqual(
            planned_sanity_checks([self._sanity_test_plan()]),
            [
                EMPTY_OUTPUT_STABILITY_CHECK,
                UNICODE_OUTPUT_STABILITY_CHECK,
                SUMMARY_RUNTIME_THRESHOLD_CHECK,
                BOUNDARY_COVERAGE_CHECK,
            ],
        )
        self.assertEqual(
            planned_sanity_checks([self._sanity_test_plan(sample=True, sample_output_text="ok\n")]),
            [
                EMPTY_OUTPUT_STABILITY_CHECK,
                UNICODE_OUTPUT_STABILITY_CHECK,
                SUMMARY_RUNTIME_THRESHOLD_CHECK,
                BOUNDARY_COVERAGE_CHECK,
                CUSTOM_SAMPLE_OUTPUT_CHECK,
            ],
        )

    def test_runtime_threshold_columns_include_main_correct_source(self) -> None:
        from app.service.verification.runtime_threshold import evaluate_summary_runtime_threshold
        from app.service.verification.lifecycle import TASK_MAIN_CORRECT, TASK_SOLUTION_RUN
        from app.service.verification.workflow_policy import (
            runtime_threshold_columns_from_tasks,
        )

        def row(
            *,
            task_id: str,
            task_kind: str,
            source_path: str,
            program_id: str,
        ) -> dict[str, object]:
            return {
                "id": task_id,
                "verification_id": "ver-runtime-columns",
                "predecessor_task_id": "",
                "task_kind": task_kind,
                "source_path": source_path,
                "program_id": program_id,
                "test_name": "001.in",
                "expected_behavior": "accepted",
                "queue_index": 1,
                "status": VerificationTaskStatus.DONE,
                "verdict": "OK",
                "run_id": "",
                "judgehost_task_id": "",
                "runtime_sec": 0.6,
                "cpu_sec": 0.6,
                "wall_sec": 0.6,
                "memory_kb": 1024,
                "answer_correct": True,
                "compile_log": "",
                "diagnostics_json": "[]",
                "error_text": "",
                "feedback_text": "",
                "output_ref": "",
                "started_at": "",
                "finished_at": "",
                "created_at": "",
                "updated_at": "",
            }

        columns = runtime_threshold_columns_from_tasks(
            artifact_verification_id="ver-runtime-columns",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                self._verification_program(
                    program_id="accepted",
                    source_path="solutions/std.cpp",
                    expected_behavior="accepted",
                    kind=TASK_MAIN_CORRECT,
                ),
                self._verification_program(
                    program_id="solution-0",
                    source_path="solutions/other.cpp",
                    expected_behavior="accepted",
                    kind=TASK_SOLUTION_RUN,
                ),
            ],
            rows=[
                row(
                    task_id="vt-main",
                    task_kind=TASK_MAIN_CORRECT,
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                ),
                row(
                    task_id="vt-solution",
                    task_kind=TASK_SOLUTION_RUN,
                    source_path="solutions/other.cpp",
                    program_id="solution-0",
                ),
            ],
            test_names=["001.in"],
            fail_flag=False,
        )

        by_source = {str(column["source"]): column for column in columns}
        self.assertIn("solutions/std.cpp", by_source)
        report = evaluate_summary_runtime_threshold(
            summary=dict(by_source["solutions/std.cpp"]["summary"]),
            source="solutions/std.cpp",
            time_limit_ms=1000,
        )
        self.assertIsNotNone(report.warning_hit)

    def test_effective_verification_kind_uses_full_available_test_set(self) -> None:
        from app.service.verification.workflow_policy import effective_verification_kind
        from app.service.verification.types import Kind

        self.assertEqual(
            effective_verification_kind(
                sample_only=True,
                requested_test_names=["001.in"],
                available_test_names=["001.in", "002.in"],
            ),
            Kind.SAMPLE.value,
        )
        self.assertEqual(
            effective_verification_kind(
                sample_only=False,
                requested_test_names=["001.in", "002.in"],
                available_test_names=["001.in", "002.in", "003.in"],
            ),
            Kind.CUSTOM.value,
        )
        self.assertEqual(
            effective_verification_kind(
                sample_only=False,
                requested_test_names=["001.in", "002.in", "003.in"],
                available_test_names=["001.in", "002.in", "003.in"],
            ),
            Kind.ALL.value,
        )

    def test_non_all_verification_skips_sanity_plan(self) -> None:
        from app.service.verification.workflow_policy import sanity_plan_for_verification_kind
        from app.service.verification.types import Kind

        test_plans = [self._sanity_test_plan(sample=True, sample_output_text="ok\n")]

        checks, status = sanity_plan_for_verification_kind(Kind.SAMPLE.value, test_plans)
        self.assertEqual(checks, [])
        self.assertEqual(status, "skipped")

        checks, status = sanity_plan_for_verification_kind(Kind.CUSTOM.value, test_plans)
        self.assertEqual(checks, [])
        self.assertEqual(status, "skipped")

        checks, status = sanity_plan_for_verification_kind(Kind.ALL.value, test_plans)
        self.assertEqual(status, "pending")
        self.assertIn("empty_output_stability", checks)
        self.assertIn("custom_sample_output", checks)

    def test_build_graph_creates_per_source_per_test_nodes(self) -> None:
        from app.service.verification.workflow_policy import build_graph
        from app.service.verification.plan import VerificationTestPlan

        verification_id = canonical_test_verification_id("graph-natural-keys")
        source_file_by_path = {
            "solutions/accepted.cpp": self.runtime_blob_store.put_bytes(
                b"int main(){return 0;}\n"
            ),
            "solutions/wa.cpp": self.runtime_blob_store.put_bytes(
                b"int main(){return 1;}\n"
            ),
        }
        graph = build_graph(
            verification_id=verification_id,
            accepted_source_path="solutions/accepted.cpp",
            source_file_by_path=source_file_by_path,
            test_plan_by_name={
                "001.in": VerificationTestPlan(
                    test_name="001.in",
                    source_kind="gen",
                    display_source_path="generators/gen.cpp",
                    execution_source_name="gen.cpp",
                    execution_source_file=self.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
                    execution_input_file=self.runtime_blob_store.put_bytes(b"\"$SUBMISSION_BIN\"\n"),
                    extra_source_files={},
                    tests_meta={},
                    sample=False,
                    sample_input_custom=False,
                    sample_input_text="",
                    uses_custom_sample_input=False,
                    sample_output_text="",
                    sample_output_validate=True,
                ),
                "002.in": VerificationTestPlan(
                    test_name="002.in",
                    source_kind="gen",
                    display_source_path="generators/gen.cpp",
                    execution_source_name="gen.cpp",
                    execution_source_file=self.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
                    execution_input_file=self.runtime_blob_store.put_bytes(b"\"$SUBMISSION_BIN\"\n"),
                    extra_source_files={},
                    tests_meta={},
                    sample=False,
                    sample_input_custom=False,
                    sample_input_text="",
                    uses_custom_sample_input=False,
                    sample_output_text="",
                    sample_output_validate=True,
                ),
            },
            targets=[
                {
                    "path": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "program_id": "accepted",
                },
                {
                    "path": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "program_id": "solution-0",
                },
            ],
            test_names=["001.in", "002.in"],
        )
        generate = [row for row in graph.tasks if row.task_kind == "generate-input"]
        main = [row for row in graph.tasks if row.task_kind == "main-correct"]
        solution = [row for row in graph.tasks if row.task_kind == "solution-run"]
        self.assertEqual(len(generate), 2)
        self.assertEqual(len(main), 2)
        self.assertEqual(len(solution), 2)
        self.assertEqual(len(graph.edges), 5)
        self.assertEqual(
            [program.program_id for program in graph.programs],
            ["accepted", "solution-0", "generator-0"],
        )
        self.assertEqual({row.source_path for row in generate}, {"generators/gen.cpp"})
        self.assertEqual({row.program_id for row in generate}, {"generator-0"})
        self.assertEqual({row.program_id for row in main}, {"accepted"})
        self.assertEqual({row.program_id for row in solution}, {"solution-0"})
        self.assertEqual(generate[0].result.verdict, "")
        self.assertEqual(generate[1].result.verdict, "SK")
        self.assertEqual(
            generate[1].result.feedback_text,
            "duplicate generator invocation; skipped, same as 001.in",
        )
        self.assertIn((generate[0].task_id, generate[1].task_id), graph.edges)
        self.assertEqual(
            generate[0].task_id,
            verification_task_id(
                verification_id,
                "generator-0",
                "001.in",
            ),
        )
        self.assertEqual(solution[0].program_id, "solution-0")

    def test_natural_task_id_accepts_longest_legal_test_name(self) -> None:
        from app.main_constant import RUN_TEST_NAME_RE

        test_name = "a" + ("b" * 127) + ".in"
        self.assertEqual(len(test_name), 131)
        self.assertIsNotNone(RUN_TEST_NAME_RE.fullmatch(test_name))

        task_id = verification_task_id(
            canonical_test_verification_id("longest-test-name"),
            "accepted",
            test_name,
        )

        self.assertTrue(task_id.endswith(f"~accepted~{test_name}"))

    def test_verification_summary_from_tasks_preserves_cancelled_parent(self) -> None:
        from app.service.verification.workflow_policy import verification_summary_from_tasks

        rows = [
            {
                "id": "vt-solution",
                "verification_id": "ver-cancel-summary",
                "task_kind": "solution-run",
                "source_path": "solutions/a.cpp",
                "program_id": "solution-0",
                "test_name": "001.in",
                "expected_behavior": "accepted",
                "queue_index": 1,
                "status": VerificationTaskStatus.CANCELLED,
                "verdict": "",
                "run_id": "",
                "judgehost_task_id": "",
                "runtime_sec": None,
                "cpu_sec": None,
                "wall_sec": None,
                "memory_kb": None,
                "compile_log": "",
                "diagnostics_json": "[]",
                "error_text": "",
                "feedback_text": "",
                "output_ref": "",
                "started_at": "2026-03-23T00:00:00Z",
                "finished_at": "2026-03-23T00:00:01Z",
                "cancel_reason": "verification cancelled by user",
                "created_at": "2026-03-23T00:00:00Z",
                "updated_at": "2026-03-23T00:00:01Z",
            }
        ]
        status, summary, counts = verification_summary_from_tasks(
            verification_id="ver-cancel-summary",
            artifact_verification_id="ver-cancel-summary",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                self._verification_program(
                    program_id="solution-0",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                    kind="solution-run",
                )
            ],
            rows=rows,
            test_names=["001.in"],
            parent_status=VerificationStatus.CANCELLED,
            fail_reason="",
            display_limit=65536,
        )
        self.assertEqual(status, "cancelled")
        self.assertEqual(str(summary["status"]), "cancelled")
        self.assertEqual(int(counts["cancelled"]), 1)

    def test_verification_summary_from_tasks_excludes_main_correct_runs_from_solution_columns(self) -> None:
        from app.service.verification.workflow_policy import verification_summary_from_tasks

        rows = [
            {
                "id": "vt-main",
                "verification_id": "ver-graph-summary",
                "task_kind": "main-correct",
                "source_path": "solutions/accepted.cpp",
                "program_id": "accepted",
                "test_name": "001.in",
                "expected_behavior": "accepted",
                "queue_index": 1,
                "status": VerificationTaskStatus.DONE,
                "verdict": "AC",
                "run_id": "r-main-task",
                "judgehost_task_id": "jt-main",
                "runtime_sec": 0.01,
                "cpu_sec": 0.01,
                "wall_sec": 0.01,
                "memory_kb": 1,
                "compile_log": "",
                "diagnostics_json": "[]",
                "error_text": "",
                "feedback_text": "",
                "output_ref": "",
                "started_at": None,
                "finished_at": None,
                "cancel_reason": "",
                "created_at": "",
                "updated_at": "",
            },
            {
                "id": "vt-solution",
                "verification_id": "ver-graph-summary",
                "task_kind": "solution-run",
                "source_path": "solutions/wa.cpp",
                "program_id": "solution-0",
                "test_name": "001.in",
                "expected_behavior": "wrong_answer",
                "queue_index": 2,
                    "status": VerificationTaskStatus.LEASED,
                "verdict": "",
                "run_id": "",
                "judgehost_task_id": "",
                "runtime_sec": None,
                "cpu_sec": None,
                "wall_sec": None,
                "memory_kb": None,
                "compile_log": "",
                "diagnostics_json": "[]",
                "error_text": "",
                "feedback_text": "",
                "output_ref": "",
                "started_at": None,
                "finished_at": None,
                "cancel_reason": "",
                "created_at": "",
                "updated_at": "",
            },
        ]
        status, summary, counts = verification_summary_from_tasks(
            verification_id="ver-graph-summary",
            artifact_verification_id="ver-artifact-summary",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                self._verification_program(
                    program_id="accepted",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                    kind="main-correct",
                ),
                self._verification_program(
                    program_id="solution-0",
                    source_path="solutions/wa.cpp",
                    expected_behavior="wrong_answer",
                    kind="solution-run",
                ),
            ],
            rows=rows,
            test_names=["001.in"],
            parent_status=VerificationStatus.RUNNING,
            fail_reason="",
            display_limit=65536,
        )
        self.assertEqual(status, "running")
        self.assertEqual(int(counts["total"]), 2)
        self.assertTrue(bool(summary.get("task_graph")))
        self.assertEqual(summary.get("source_paths"), ["solutions/wa.cpp"])

    def test_truncated_metadata_helpers_mark_oversized_values(self) -> None:
        compile_meta = canonical_truncated_text("x" * 32, limit=8)
        diagnostics_meta = canonical_diagnostics(
            [{"level": "error", "message": "a" * 64}],
            list_limit=1,
            message_limit=10,
        )
        diagnostics_json = diagnostics_json_text(diagnostics_meta["rows"])
        self.assertLessEqual(len(str(compile_meta["text"]).encode("utf-8")), 8)
        self.assertTrue(str(compile_meta["text"]).endswith("..."))
        self.assertTrue(bool(compile_meta["truncated"]))
        self.assertEqual(int(diagnostics_meta["total"]), 1)
        self.assertTrue(bool(diagnostics_meta["rows"][0].get("message_truncated")))
        self.assertIn('"message":"aaaaaaa..."', diagnostics_json)

    def test_late_diagnostic_limit_counts_serialized_json_escaping(self) -> None:
        item = new_task_diagnostic_item(
            kind="debug-info",
            hostname="judgehost-1",
            text=('line "quoted"\\path\n' * 64),
            received_at="2026-08-10T00:00:00+00:00",
            limit_bytes=4096,
        )

        merged, outcome = merge_task_diagnostic_snapshot(
            TaskDiagnosticSnapshot(),
            item,
            limit_bytes=256,
        )

        self.assertEqual(outcome, "persisted")
        self.assertLessEqual(
            len(task_diagnostic_snapshot_json(merged).encode("utf-8")),
            256,
        )
        self.assertTrue(merged.items[0].text.endswith("..."))
        unchanged, tiny_outcome = merge_task_diagnostic_snapshot(
            merged,
            new_task_diagnostic_item(
                kind="internal-error",
                hostname="judgehost-2",
                text="new context",
                received_at="2026-08-10T00:00:01+00:00",
                limit_bytes=32,
            ),
            limit_bytes=1,
        )
        self.assertEqual(tiny_outcome, "not-applicable")
        self.assertEqual(unchanged, merged)
