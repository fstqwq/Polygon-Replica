from __future__ import annotations

from unittest.mock import patch

from app.service.verification.lifecycle import (
    VerificationCompileSpec,
    VerificationProgram,
)
from app.service.verification.types import VerificationTaskStatus

from tests.common import E2ETestBase, runtime
from tests.identity_helpers import canonical_test_verification_id
from tests.verification_adapter_fixture import (
    sanity_test_plan,
    task_row,
    verification_program,
)


class TestVerificationAdapters(E2ETestBase):
    def test_required_verification_file_waits_for_late_artifact_visibility(self) -> None:
        from app.service.verification.workflow import _verification_required_file

        payload = runtime.runtime_blob_store.put_bytes(b"generated\n")
        calls = {"ref": 0, "descriptor": 0}

        def _late_ref(_verification_id: str, _test_name: str, _ref_key: str) -> str:
            calls["ref"] += 1
            return str(payload.blob_ref) if calls["ref"] >= 2 else ""

        def _late_descriptor(_ref: str) -> object:
            calls["descriptor"] += 1
            return payload if calls["descriptor"] >= 2 else None

        with patch.object(runtime.verification_service, "verification_artifact_ref", side_effect=_late_ref), patch.object(
            runtime.runtime_blob_store,
            "descriptor",
            side_effect=_late_descriptor,
        ):
            self.assertEqual(
                _verification_required_file(
                    "ver-late",
                    "026.in",
                    "input_ref",
                    label="verification test 026.in",
                    timeout_sec=0.2,
                    interval_sec=0.001,
                    verification_service=runtime.verification_service,
                    runtime_blob_store=runtime.runtime_blob_store,
                ),
                payload,
            )

        self.assertGreaterEqual(calls["ref"], 3)
        self.assertGreaterEqual(calls["descriptor"], 2)

    def test_task_publish_forwards_bypass_case_result_cache_to_judgehost(self) -> None:
        from app.service.verification.lifecycle import TASK_GENERATE_INPUT, TASK_MAIN_CORRECT
        from app.service.verification.workflow import (
            TaskExecutionContext,
            _publish_generate_task,
            _publish_run_task,
        )

        verification_id = canonical_test_verification_id(
            self.random_id("ver-force-recompile")
        )
        source_file = runtime.runtime_blob_store.put_bytes(b"int main(){return 0;}\n")
        input_file = runtime.runtime_blob_store.put_bytes(b"1\n")
        test_plan = sanity_test_plan()
        accepted_program = verification_program(
            program_id="accepted",
            kind=TASK_MAIN_CORRECT,
            source_path="solutions/std.cpp",
            expected_behavior="accepted",
        )
        generator_program = VerificationProgram(
            program_id="generator-0",
            kind=TASK_GENERATE_INPUT,
            source_path=test_plan.display_source_path,
            compile_spec=VerificationCompileSpec(
                source_name=test_plan.execution_source_name,
                source_file=test_plan.execution_source_file,
                extra_source_files=tuple(
                    sorted(test_plan.extra_source_files.items())
                ),
                manual_validate_only=True,
            ),
            expected_behavior="accepted",
        )
        execution = TaskExecutionContext(
            problem=self.problem,
            user=self.user,
            verification_id=verification_id,
            mode="pass-fail",
            pass_limit=1,
            snapshot_root=source_file.path.parent,
            artifact_file_by_test_ref={},
            program_by_id={
                accepted_program.program_id: accepted_program,
                generator_program.program_id: generator_program,
            },
            execution_template_by_program_id={},
            test_plan_by_name={"001.in": test_plan},
            run_verification_payload_base={},
            generate_verification_payload_base={},
            bypass_case_result_cache=True,
            judgehost=runtime.judgehost_task_service,
            runtime_blob_store=runtime.runtime_blob_store,
            verification_service=runtime.verification_service,
            task_store=runtime.verification_task_store,
        )
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-force-{len(calls)}"

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "prepare_execution_template",
            return_value={},
        ) as prepare_template, patch(
            "app.service.verification.workflow._verification_required_file",
            return_value=input_file,
        ):
            _publish_generate_task(
                task_row(
                    "vt-generate",
                    task_kind=TASK_GENERATE_INPUT,
                    status=VerificationTaskStatus.PENDING,
                    queue_index=1,
                    source_path=test_plan.display_source_path,
                    program_id="generator-0",
                ),
                execution=execution,
                test_plan=test_plan,
            )
            _publish_run_task(
                task_row(
                    "vt-main",
                    task_kind=TASK_MAIN_CORRECT,
                    status=VerificationTaskStatus.PENDING,
                    queue_index=2,
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                ),
                execution=execution,
            )
            _publish_run_task(
                task_row(
                    "vt-main-repeat",
                    task_kind=TASK_MAIN_CORRECT,
                    status=VerificationTaskStatus.PENDING,
                    queue_index=3,
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                ),
                execution=execution,
            )
            skipped_row = task_row(
                "vt-main-skipped",
                task_kind=TASK_MAIN_CORRECT,
                status=VerificationTaskStatus.PENDING,
                queue_index=4,
                source_path="solutions/std.cpp",
                program_id="accepted",
            )
            skipped_row["verdict"] = "SK"
            skipped = _publish_run_task(skipped_row, execution=execution)

        self.assertEqual([call["bypass_case_result_cache"] for call in calls], [True, True, True])
        self.assertEqual(prepare_template.call_count, 2)
        self.assertIsNotNone(skipped.terminal_result)
        assert skipped.terminal_result is not None
        self.assertEqual(skipped.terminal_result.verdict, "SK")
        self.assertEqual(len(calls), 3)

    def test_sanity_stability_probes_pass_on_non_ac_non_fl(self) -> None:


        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-stable")
        )
        logs_dir = runtime.storage_layout.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []
        closed_programs: list[tuple[str, list[str]]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-{len(calls)}"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            return {
                "summary": {
                    "tests": [
                        {
                            "test": test_name,
                            "verdict": "WA",
                            "message": f"{task_id} rejected",
                        }
                    ]
                }
            }

        def _fake_close_programs(
            closed_verification_id: str,
            program_ids: list[str],
        ) -> None:
            closed_programs.append((closed_verification_id, list(program_ids)))

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            runtime.judgehost_task_service,
            "close_programs",
            side_effect=_fake_close_programs,
        ):
            result = runtime.verification_sanity_service.run(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[sanity_test_plan()],
                bypass_case_result_cache=True,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 2)
        self.assertEqual([str(call["upload_filename"]) for call in calls], ["sanity_empty_output.py", "sanity_unicode_output.py"])
        self.assertTrue(all(call["persist_verification_run"] is False for call in calls))
        self.assertTrue(all(call["selected_tests"] == ["001.in"] for call in calls))
        self.assertTrue(all(call["expected_behavior"] == "unknown" for call in calls))
        self.assertTrue(all(call["bypass_case_result_cache"] is True for call in calls))
        self.assertEqual(
            [str(call["verification_program_id"]) for call in calls],
            [
                "sanity-empty_output_stability",
                "sanity-unicode_output_stability",
            ],
        )
        self.assertEqual(
            closed_programs,
            [
                (verification_id, ["sanity-empty_output_stability"]),
                (verification_id, ["sanity-unicode_output_stability"]),
            ],
        )
        self.assertIn("empty_output_stability 001.in: ok - WA", (logs_dir / "stability.log").read_text(encoding="utf-8"))

    def test_sanity_boundary_coverage_warning_keeps_verification_ok(self) -> None:
        from app.service.verification.sanity import BOUNDARY_COVERAGE_CHECK

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-boundary")
        )
        logs_dir = runtime.storage_layout.prepare_verification_root(verification_id).resolve() / "logs"

        def _fake_enqueue_task(**kwargs: object) -> str:
            return "jt-boundary"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "WA", "message": "rejected"}]}}

        feedback = (
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
        )
        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = runtime.verification_sanity_service.run(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[sanity_test_plan()],
                generate_feedback_by_test={"001.in": feedback},
            )

        self.assertEqual(result.status, "warning")
        self.assertEqual(result.check_name, BOUNDARY_COVERAGE_CHECK)
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.error, "Test data did not hit: n max=3")
        boundary_result = next(item for item in result.check_results if item.name == BOUNDARY_COVERAGE_CHECK)
        self.assertEqual(boundary_result.status, "warning")
        self.assertEqual([message.message for message in boundary_result.messages], ["Test data did not hit: n max=3"])
        self.assertIn("Test data did not hit: n max=3", (logs_dir / "boundary.log").read_text(encoding="utf-8"))

    def test_sanity_runtime_threshold_warning_uses_answer_correct_summary(self) -> None:
        from app.service.verification.sanity import BOUNDARY_COVERAGE_CHECK, SUMMARY_RUNTIME_THRESHOLD_CHECK

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-runtime")
        )
        logs_dir = runtime.storage_layout.prepare_verification_root(verification_id).resolve() / "logs"

        def _fake_enqueue_task(**kwargs: object) -> str:
            return "jt-runtime"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "WA", "message": "rejected"}]}}

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = runtime.verification_sanity_service.run(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[sanity_test_plan(test_name="001.in"), sanity_test_plan(test_name="002.in")],
                runtime_columns=[
                    {
                        "source": "solutions/accepted.cpp",
                        "summary_has_tl": False,
                        "summary": {
                            "tests": [
                                {"test": "001.in", "verdict": "OK", "time_user_ms": 600, "answer_correct": True},
                                {"test": "002.in", "verdict": "OK", "time_user_ms": 1200, "answer_correct": True},
                            ]
                        },
                    },
                    {
                        "source": "solutions/tle.cpp",
                        "summary_has_tl": True,
                        "summary": {
                            "tests": [
                                {"test": "001.in", "verdict": "TL", "time_user_ms": 600, "answer_correct": True},
                                {"test": "002.in", "verdict": "TL", "time_user_ms": 1200, "answer_correct": True},
                            ]
                        },
                    }
                ],
                time_limit_ms=1000,
            )

        self.assertEqual(result.status, "warning")
        self.assertEqual(result.check_name, SUMMARY_RUNTIME_THRESHOLD_CHECK)
        self.assertEqual(result.error, "solutions/accepted.cpp: accepted solution is close to the time limit.")
        runtime_result = next(item for item in result.check_results if item.name == SUMMARY_RUNTIME_THRESHOLD_CHECK)
        self.assertEqual(runtime_result.status, "warning")
        self.assertEqual(
            [message.message for message in runtime_result.messages],
            [
                "solutions/accepted.cpp: accepted solution is close to the time limit.",
                "solutions/tle.cpp: correct output in 50% extra time limit.",
            ],
        )
        boundary_result = next(item for item in result.check_results if item.name == BOUNDARY_COVERAGE_CHECK)
        self.assertEqual(boundary_result.status, "passed")

    def test_sanity_stability_probe_failure_does_not_skip_later_checks(self) -> None:
        from app.service.verification.sanity import EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-ac")
        )
        logs_dir = runtime.storage_layout.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-{len(calls)}"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "OK", "message": "accepted"}]}}

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = runtime.verification_sanity_service.run(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[sanity_test_plan()],
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.check_name, EMPTY_OUTPUT_STABILITY_CHECK)
        self.assertEqual(result.checked_count, 0)
        self.assertIn("got OK", result.error)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item.name for item in result.check_results[:2]], [EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK])
        self.assertEqual([item.status for item in result.check_results[:2]], ["failed", "failed"])

    def test_sanity_stability_probe_fails_on_unicode_fl(self) -> None:
        from app.service.verification.sanity import UNICODE_OUTPUT_STABILITY_CHECK

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-fl")
        )
        logs_dir = runtime.storage_layout.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-{len(calls)}"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            verdict = "WA" if task_id == "jt-1" else "FL"
            return {"summary": {"tests": [{"test": test_name, "verdict": verdict, "message": "unicode crash"}]}}

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            runtime.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = runtime.verification_sanity_service.run(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[sanity_test_plan()],
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.check_name, UNICODE_OUTPUT_STABILITY_CHECK)
        self.assertEqual(result.checked_count, 1)
        self.assertIn("got FL", result.error)
        self.assertEqual(len(calls), 2)

    def test_custom_run_upload_preserves_source_extension_for_compile_template(
        self,
    ) -> None:
        from app.service.verification.workflow import (
            TaskExecutionContext,
            _execution_template,
        )
        from app.service.verification.workflow_policy import build_graph

        for source_name, source_content in (
            ("foo.cpp", b"int main(){return 0;}\n"),
            ("Main.java", b"class Main { public static void main(String[] a) {} }\n"),
        ):
            with self.subTest(source_name=source_name):
                accepted_path = "solutions/accepted.cpp"
                uploaded_path = f"uploads/solution-0/{source_name}"
                accepted_file = runtime.runtime_blob_store.put_bytes(
                    b"int main(){return 0;}\n"
                )
                uploaded_file = runtime.runtime_blob_store.put_bytes(
                    source_content
                )
                graph = build_graph(
                    verification_id=canonical_test_verification_id(
                        f"custom-upload-{source_name}"
                    ),
                    accepted_source_path=accepted_path,
                    source_file_by_path={
                        accepted_path: accepted_file,
                        uploaded_path: uploaded_file,
                    },
                    test_plan_by_name={},
                    targets=[
                        {
                            "path": accepted_path,
                            "expected_behavior": "accepted",
                            "program_id": "accepted",
                        },
                        {
                            "path": uploaded_path,
                            "expected_behavior": "unknown",
                            "program_id": "solution-0",
                        },
                    ],
                    test_names=[],
                )
                program = next(
                    item
                    for item in graph.programs
                    if item.program_id == "solution-0"
                )
                execution = TaskExecutionContext(
                    problem=self.problem,
                    user=self.user,
                    verification_id=canonical_test_verification_id(
                        f"custom-upload-execution-{source_name}"
                    ),
                    mode="pass-fail",
                    pass_limit=1,
                    snapshot_root=accepted_file.path.parent,
                    artifact_file_by_test_ref={},
                    program_by_id={program.program_id: program},
                    execution_template_by_program_id={},
                    test_plan_by_name={},
                    run_verification_payload_base={},
                    generate_verification_payload_base={},
                    bypass_case_result_cache=False,
                    judgehost=runtime.judgehost_task_service,
                    runtime_blob_store=runtime.runtime_blob_store,
                    verification_service=runtime.verification_service,
                    task_store=runtime.verification_task_store,
                )
                observed: dict[str, str] = {}

                def _prepare_template(**kwargs: object) -> dict[str, object]:
                    observed["source_name"] = str(
                        kwargs.get("upload_filename") or ""
                    )
                    return {}

                with patch.object(
                    runtime.judgehost_task_service,
                    "prepare_execution_template",
                    side_effect=_prepare_template,
                ):
                    _execution_template(execution, program=program)

                self.assertEqual(observed.get("source_name"), source_name)
