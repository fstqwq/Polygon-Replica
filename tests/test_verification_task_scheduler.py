from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

# tests.common installs isolated /tmp paths before app modules can create runtime config.
from tests.common import (
    E2ETestBase,
    clear_activation_task_abort_fault,
    clear_completion_ref_abort_fault,
    config,
    install_activation_task_abort_fault,
    install_completion_ref_abort_fault,
)
from tests.identity_helpers import canonical_test_verification_id
from tests.db_helpers import (
    db_fetch_all,
    verification_programs_for_tasks,
)

from app.service.judgehost.case_result import (
    CaseTerminalReport,
    build_case_terminal_report,
)
from app.service.verification.task_metadata import canonical_diagnostics, canonical_truncated_text, diagnostics_json_text
from app.service.verification.task_completion import (
    TaskCompletion,
)
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    SanityFinish,
    VerificationCompileSpec,
    VerificationProgram,
    VerificationAdmission,
    verification_task_id,
)
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)
from app.service.verification.diagnostic import (
    TaskDiagnosticSnapshot,
    merge_task_diagnostic_snapshot,
    new_task_diagnostic_item,
    task_diagnostic_snapshot_json,
)


def _verification_program(
    *,
    program_id: str,
    kind: str,
    source_path: str,
    expected_behavior: str,
) -> VerificationProgram:
    return VerificationProgram(
        program_id=program_id,
        kind=kind,
        source_path=source_path,
        compile_spec=VerificationCompileSpec(
            source_name=Path(source_path).name,
            source_file=config.runtime_blob_store.put_bytes(
                b"int main(){return 0;}\n"
            ),
        ),
        expected_behavior=expected_behavior,
    )


def _execution_result(
    *,
    verdict: str,
    output_ref: str = "",
    feedback: str = "",
    error: str = "",
    compile_log: str = "",
    diagnostics: list[dict[str, object]] | None = None,
    answer_correct: bool = False,
) -> ExecutionResult:
    passes = ()
    if output_ref:
        artifact_ref = "blob://sha256/" + "a" * 64
        passes = (
            ExecutionPassResult(
                number=1,
                capture_status=CAPTURE_COMPLETE,
                runresult="correct",
                verdict=verdict,
                score_text="",
                answer_correct=answer_correct,
                usage=ExecutionUsage(0.01, 0.01, 0.01, 1),
                feedback=feedback,
                artifacts=PassArtifacts(
                    input_ref=artifact_ref,
                    output_ref=output_ref,
                    stderr_ref=artifact_ref,
                    system_ref=artifact_ref,
                    judge_message_ref=artifact_ref,
                    team_message_ref=artifact_ref,
                    metadata_ref=artifact_ref,
                    compare_metadata_ref=artifact_ref,
                ),
            ),
        )
    return normalize_execution_result(
        passes=passes,
        verdict=verdict,
        answer_correct=answer_correct,
        feedback=feedback,
        error=error,
        compile_log=compile_log,
        compile_diagnostics=() if diagnostics is None else diagnostics,
    )


def _terminal_report(
    *,
    judgehost_task_id: str,
    verification_id: str,
    run_id: str,
    result: ExecutionResult,
    status: str = "ok",
    missing_case_result: bool = False,
    summary: dict[str, object] | None = None,
) -> CaseTerminalReport:
    return build_case_terminal_report(
        task_id=judgehost_task_id,
        verification_id=verification_id,
        run_id=run_id,
        status=status,
        task_status="done",
        error_text=result.outcome.error,
        summary=dict(summary or {}),
        missing_case_result=missing_case_result,
        execution_result=result,
    )


def _multi_pass_result(final_output_ref: str) -> ExecutionResult:
    def _pass(number: int, output_ref: str) -> ExecutionPassResult:
        prefix = f"blob://pass-{number}"
        return ExecutionPassResult(
            number=number,
            capture_status=CAPTURE_COMPLETE,
            runresult="correct",
            verdict="OK",
            score_text=str(number),
            answer_correct=number == 2,
            usage=ExecutionUsage(
                runtime_sec=number / 100,
                cpu_sec=number / 200,
                wall_sec=number / 50,
                memory_kb=number * 128,
            ),
            feedback=f"pass {number}",
            artifacts=PassArtifacts(
                input_ref=f"{prefix}/input",
                output_ref=output_ref,
                stderr_ref=f"{prefix}/stderr",
                system_ref=f"{prefix}/system",
                judge_message_ref=f"{prefix}/judge-message",
                team_message_ref=f"{prefix}/team-message",
                metadata_ref=f"{prefix}/metadata",
                compare_metadata_ref=f"{prefix}/compare-metadata",
            ),
        )

    return normalize_execution_result(
        passes=(
            _pass(1, "blob://pass-1/output"),
            _pass(2, final_output_ref),
        ),
        verdict="OK",
        answer_correct=True,
        compile_log="compiler telemetry",
        compile_diagnostics=({"level": "warning", "message": "diagnostic"},),
        warnings=("runner telemetry",),
    )


def _task_row(
    task_id: str,
    *,
    task_kind: str,
    status: str,
    queue_index: int,
    source_path: str = "solutions/a.cpp",
    program_id: str = "solution-0",
    test_name: str = "001.in",
) -> dict[str, object]:
    return {
        "id": task_id,
        "verification_id": "ver-1",
        "predecessor_task_id": "",
        "task_kind": task_kind,
        "source_path": source_path,
        "program_id": program_id,
        "test_name": test_name,
        "expected_behavior": "accepted",
        "queue_index": queue_index,
        "status": status,
        "verdict": "",
        "run_id": "",
        "judgehost_task_id": "",
        "runtime_sec": None,
        "cpu_sec": None,
        "wall_sec": None,
        "memory_kb": None,
        "answer_correct": False,
        "compile_log": "",
        "diagnostics_json": "[]",
        "error_text": "",
        "feedback_text": "",
        "output_ref": "",
        "started_at": None,
        "finished_at": None,
        "created_at": "",
        "updated_at": "",
    }


def _sanity_test_plan(
    *,
    test_name: str = "001.in",
    sample: bool = False,
    sample_output_text: str = "",
    sample_output_validate: bool = True,
) -> VerificationTestPlan:
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="manual",
        display_source_path="manual_validate.cpp",
        execution_source_name="manual_validate.cpp",
        execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
        execution_input_file=config.runtime_blob_store.put_bytes(b"1\n"),
        extra_source_files={},
        tests_meta={},
        sample=sample,
        sample_input_custom=False,
        sample_input_text="",
        uses_custom_sample_input=False,
        sample_output_text=sample_output_text,
        sample_output_validate=sample_output_validate,
    )

class TestVerificationTaskScheduler(E2ETestBase):
    def _insert_verification_row(self, verification_id: str) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        admission = config.verification_service.admit_verification(
            VerificationAdmission(
                verification_id=verification_id,
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                signature="",
                source_commit="",
                kind="all",
            )
        )
        self.assertEqual(admission.outcome, "admitted")

    def _activate_graph(
        self,
        verification_id: str,
        *,
        tasks: list[dict[str, object]],
        edges: list[tuple[str, str]],
        detail: dict[str, object] | None = None,
    ) -> None:
        predecessor_by_child: dict[str, str] = {}
        for parent_id, child_id in edges:
            if child_id in predecessor_by_child:
                raise AssertionError(f"duplicate predecessor for {child_id}")
            predecessor_by_child[child_id] = parent_id
        planned = tuple(
            PlannedTask(
                task_id=str(item["id"]),
                predecessor_task_id=predecessor_by_child.get(str(item["id"])),
                task_kind=str(item.get("task_kind") or ""),
                source_path=str(item.get("source_path") or ""),
                program_id=str(item.get("program_id") or ""),
                test_name=str(item.get("test_name") or ""),
                expected_behavior=str(item.get("expected_behavior") or ""),
                result=normalize_execution_result(
                    verdict=str(item.get("verdict") or ""),
                    feedback=str(item.get("feedback_text") or ""),
                ),
            )
            for item in tasks
        )
        accepted_completion: TaskCompletion | None = None
        if not any(task.program_id == "accepted" for task in planned):
            accepted_test_name = planned[0].test_name
            accepted_task_id = verification_task_id(
                verification_id,
                "accepted",
                accepted_test_name,
            )
            planned = (
                PlannedTask(
                    task_id=accepted_task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name=accepted_test_name,
                    expected_behavior="accepted",
                ),
                *planned,
            )
            accepted_completion = TaskCompletion(
                task_id=accepted_task_id,
                status=VerificationTaskStore.TASK_DONE,
                run_id="",
                judgehost_task_id="",
                result=normalize_execution_result(verdict="OK"),
            )
        activation = config.verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail=dict(detail or {}),
                programs=verification_programs_for_tasks(planned),
                tasks=planned,
            )
        )
        self.assertEqual(activation.outcome, "activated")
        if accepted_completion is not None:
            config.verification_task_store.commit_task_completions(
                (accepted_completion,)
            )
        for task_index, item in enumerate(tasks):
            initial_status = str(
                item.get("status") or VerificationTaskStore.TASK_PENDING
            )
            if initial_status not in {
                VerificationTaskStore.TASK_QUEUED,
                VerificationTaskStore.TASK_LEASED,
            }:
                continue
            task_id = str(item["id"])
            bound = config.verification_task_store.bind_and_expose_judgehost_runtime(
                task_id,
                run_id=str(item.get("run_id") or f"r-test-{task_index}"),
                judgehost_task_id=str(
                    item.get("judgehost_task_id") or f"jt-{task_id}"
                ),
                expose=lambda: None,
            )
            self.assertTrue(bound)
            if initial_status == VerificationTaskStore.TASK_LEASED:
                config.verification_task_store.set_task_leased(task_id)

    def test_memory_limit_is_canonical_before_verification_payloads(self) -> None:
        from app.impl.workspace.verification_dag_plan import (
            _generate_payload_base,
            _problem_limits,
            _run_payload_base,
        )
        from app.service.verification.runtime import load_problem_runtime_config

        cases = (
            ({"memory_limit_mb": 1}, 1),
            ({"memory_limit_mb": 0}, 1),
            ({"memory_limit_mb": "invalid"}, 1024),
            ({}, 1024),
            ({"memory_limit_mb": 4096}, 2048),
        )
        for problem_config, expected in cases:
            with self.subTest(problem_config=problem_config), tempfile.TemporaryDirectory() as tmp:
                snapshot = Path(tmp)
                (snapshot / "config").mkdir()
                (snapshot / "config" / "problem.json").write_text(
                    json.dumps(problem_config),
                    encoding="utf-8",
                )
                runtime = load_problem_runtime_config(
                    snapshot,
                    default_time_limit_ms=2000,
                    default_memory_limit_mb=1024,
                    default_mode="pass-fail",
                    min_time_limit_ms=100,
                    max_time_limit_ms=30000,
                    min_memory_limit_mb=1,
                    max_memory_limit_mb=2048,
                )
                self.assertEqual(runtime["memory_limit_mb"], expected)

        limits = _problem_limits(
            {"time_limit_ms": 2000, "memory_limit_mb": 1},
            pass_limit=1,
        )
        run_payload = _run_payload_base(
            build_cfg={},
            problem_limits=limits,
            source_files={},
        )
        generate_payload = _generate_payload_base(
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

    def test_required_verification_file_waits_for_late_artifact_visibility(self) -> None:
        from app.impl.workspace.verification_dag import _verification_required_file

        payload = config.runtime_blob_store.put_bytes(b"generated\n")
        calls = {"ref": 0, "descriptor": 0}

        def _late_ref(_verification_id: str, _test_name: str, _ref_key: str) -> str:
            calls["ref"] += 1
            return str(payload.blob_ref) if calls["ref"] >= 2 else ""

        def _late_descriptor(_ref: str) -> object:
            calls["descriptor"] += 1
            return payload if calls["descriptor"] >= 2 else None

        with patch.object(config.verification_service, "verification_artifact_ref", side_effect=_late_ref), patch.object(
            config.runtime_blob_store,
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
                ),
                payload,
            )

        self.assertGreaterEqual(calls["ref"], 3)
        self.assertGreaterEqual(calls["descriptor"], 2)

    def test_effective_verification_status_waits_for_pending_sanity_checks(self) -> None:
        from app.impl.workspace.sanity_checks import effective_verification_status

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
        from app.impl.workspace.sanity_checks import (
            BOUNDARY_COVERAGE_CHECK,
            CUSTOM_SAMPLE_OUTPUT_CHECK,
            EMPTY_OUTPUT_STABILITY_CHECK,
            SUMMARY_RUNTIME_THRESHOLD_CHECK,
            UNICODE_OUTPUT_STABILITY_CHECK,
            planned_sanity_checks,
        )

        self.assertEqual(
            planned_sanity_checks([_sanity_test_plan()]),
            [
                EMPTY_OUTPUT_STABILITY_CHECK,
                UNICODE_OUTPUT_STABILITY_CHECK,
                SUMMARY_RUNTIME_THRESHOLD_CHECK,
                BOUNDARY_COVERAGE_CHECK,
            ],
        )
        self.assertEqual(
            planned_sanity_checks([_sanity_test_plan(sample=True, sample_output_text="ok\n")]),
            [
                EMPTY_OUTPUT_STABILITY_CHECK,
                UNICODE_OUTPUT_STABILITY_CHECK,
                SUMMARY_RUNTIME_THRESHOLD_CHECK,
                BOUNDARY_COVERAGE_CHECK,
                CUSTOM_SAMPLE_OUTPUT_CHECK,
            ],
        )

    def test_task_publish_forwards_bypass_case_result_cache_to_judgehost(self) -> None:
        from app.impl.workspace.verification_dag import (
            TASK_GENERATE_INPUT,
            TASK_MAIN_CORRECT,
            TaskExecutionContext,
            _publish_generate_task,
            _publish_run_task,
        )

        verification_id = canonical_test_verification_id(
            self.random_id("ver-force-recompile")
        )
        source_file = config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n")
        input_file = config.runtime_blob_store.put_bytes(b"1\n")
        test_plan = _sanity_test_plan()
        accepted_program = _verification_program(
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
        )
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-force-{len(calls)}"

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "prepare_execution_template",
            return_value={},
        ) as prepare_template, patch(
            "app.impl.workspace.verification_dag._verification_required_file",
            return_value=input_file,
        ):
            _publish_generate_task(
                _task_row(
                    "vt-generate",
                    task_kind=TASK_GENERATE_INPUT,
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path=test_plan.display_source_path,
                    program_id="generator-0",
                ),
                execution=execution,
                test_plan=test_plan,
            )
            _publish_run_task(
                _task_row(
                    "vt-main",
                    task_kind=TASK_MAIN_CORRECT,
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                ),
                execution=execution,
            )
            _publish_run_task(
                _task_row(
                    "vt-main-repeat",
                    task_kind=TASK_MAIN_CORRECT,
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=3,
                    source_path="solutions/std.cpp",
                    program_id="accepted",
                ),
                execution=execution,
            )
            skipped_row = _task_row(
                "vt-main-skipped",
                task_kind=TASK_MAIN_CORRECT,
                status=VerificationTaskStore.TASK_PENDING,
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

    def test_verification_artifact_descriptors_are_cached_across_sources(self) -> None:
        from app.impl.workspace.verification_dag import _verification_required_file

        payload = config.runtime_blob_store.put_bytes(b"input\n")
        cache = {}
        with patch.object(
            config.verification_service,
            "verification_artifact_ref",
            return_value=payload.blob_ref,
        ) as lookup_ref, patch.object(
            config.runtime_blob_store,
            "descriptor",
            return_value=payload,
        ) as resolve_descriptor:
            first = _verification_required_file(
                "ver-artifact-cache",
                "001.in",
                "input_ref",
                label="test input",
                cache=cache,
            )
            second = _verification_required_file(
                "ver-artifact-cache",
                "001.in",
                "input_ref",
                label="test input",
                cache=cache,
            )
        self.assertEqual(first, payload)
        self.assertEqual(second, first)
        self.assertEqual(lookup_ref.call_count, 1)
        self.assertEqual(resolve_descriptor.call_count, 1)

    def test_sanity_stability_probes_pass_on_non_ac_non_fl(self) -> None:
        from app.impl.workspace.sanity_checks import run_verification_sanity_checks

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-stable")
        )
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"
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

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            config.judgehost_task_service,
            "close_programs",
            side_effect=_fake_close_programs,
        ):
            result = run_verification_sanity_checks(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[_sanity_test_plan()],
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
        from app.impl.workspace.sanity_checks import BOUNDARY_COVERAGE_CHECK, run_verification_sanity_checks

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-boundary")
        )
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"

        def _fake_enqueue_task(**kwargs: object) -> str:
            return "jt-boundary"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "WA", "message": "rejected"}]}}

        feedback = (
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
        )
        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = run_verification_sanity_checks(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[_sanity_test_plan()],
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
        from app.impl.workspace.sanity_checks import BOUNDARY_COVERAGE_CHECK, SUMMARY_RUNTIME_THRESHOLD_CHECK, run_verification_sanity_checks

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-runtime")
        )
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"

        def _fake_enqueue_task(**kwargs: object) -> str:
            return "jt-runtime"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "WA", "message": "rejected"}]}}

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = run_verification_sanity_checks(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[_sanity_test_plan(test_name="001.in"), _sanity_test_plan(test_name="002.in")],
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

    def test_runtime_threshold_columns_include_main_correct_source(self) -> None:
        from app.impl.workspace.runtime_threshold import evaluate_summary_runtime_threshold
        from app.impl.workspace.verification_dag import (
            TASK_MAIN_CORRECT,
            TASK_SOLUTION_RUN,
            _runtime_threshold_columns_from_tasks,
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
                "status": VerificationTaskStore.TASK_DONE,
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

        columns = _runtime_threshold_columns_from_tasks(
            artifact_verification_id="ver-runtime-columns",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                _verification_program(
                    program_id="accepted",
                    source_path="solutions/std.cpp",
                    expected_behavior="accepted",
                    kind=TASK_MAIN_CORRECT,
                ),
                _verification_program(
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

    def test_sanity_stability_probe_failure_does_not_skip_later_checks(self) -> None:
        from app.impl.workspace.sanity_checks import EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK, run_verification_sanity_checks

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-ac")
        )
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-{len(calls)}"

        def _fake_wait_for_task_case_result(_task_id: str, test_name: str) -> dict[str, object]:
            return {"summary": {"tests": [{"test": test_name, "verdict": "OK", "message": "accepted"}]}}

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = run_verification_sanity_checks(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[_sanity_test_plan()],
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.check_name, EMPTY_OUTPUT_STABILITY_CHECK)
        self.assertEqual(result.checked_count, 0)
        self.assertIn("got OK", result.error)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item.name for item in result.check_results[:2]], [EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK])
        self.assertEqual([item.status for item in result.check_results[:2]], ["failed", "failed"])

    def test_sanity_stability_probe_fails_on_unicode_fl(self) -> None:
        from app.impl.workspace.sanity_checks import UNICODE_OUTPUT_STABILITY_CHECK, run_verification_sanity_checks

        verification_id = canonical_test_verification_id(
            self.random_id("ver-sanity-fl")
        )
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-{len(calls)}"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            verdict = "WA" if task_id == "jt-1" else "FL"
            return {"summary": {"tests": [{"test": test_name, "verdict": verdict, "message": "unicode crash"}]}}

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = run_verification_sanity_checks(
                problem=self.problem,
                user=self.user,
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_dir,
                test_plans=[_sanity_test_plan()],
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.check_name, UNICODE_OUTPUT_STABILITY_CHECK)
        self.assertEqual(result.checked_count, 1)
        self.assertIn("got FL", result.error)
        self.assertEqual(len(calls), 2)

    def test_effective_verification_kind_uses_full_available_test_set(self) -> None:
        from app.impl.workspace.verification_dag import _effective_verification_kind
        from app.service.verification.types import Kind

        self.assertEqual(
            _effective_verification_kind(
                sample_only=True,
                requested_test_names=["001.in"],
                available_test_names=["001.in", "002.in"],
            ),
            Kind.SAMPLE.value,
        )
        self.assertEqual(
            _effective_verification_kind(
                sample_only=False,
                requested_test_names=["001.in", "002.in"],
                available_test_names=["001.in", "002.in", "003.in"],
            ),
            Kind.CUSTOM.value,
        )
        self.assertEqual(
            _effective_verification_kind(
                sample_only=False,
                requested_test_names=["001.in", "002.in", "003.in"],
                available_test_names=["001.in", "002.in", "003.in"],
            ),
            Kind.ALL.value,
        )

    def test_non_all_verification_skips_sanity_plan(self) -> None:
        from app.impl.workspace.verification_dag import _sanity_plan_for_verification_kind
        from app.service.verification.types import Kind

        test_plans = [_sanity_test_plan(sample=True, sample_output_text="ok\n")]

        checks, status = _sanity_plan_for_verification_kind(Kind.SAMPLE.value, test_plans)
        self.assertEqual(checks, [])
        self.assertEqual(status, "skipped")

        checks, status = _sanity_plan_for_verification_kind(Kind.CUSTOM.value, test_plans)
        self.assertEqual(checks, [])
        self.assertEqual(status, "skipped")

        checks, status = _sanity_plan_for_verification_kind(Kind.ALL.value, test_plans)
        self.assertEqual(status, "pending")
        self.assertIn("empty_output_stability", checks)
        self.assertIn("custom_sample_output", checks)

    def test_build_graph_creates_per_source_per_test_nodes(self) -> None:
        from app.impl.workspace.verification_dag import _build_graph
        from app.service.verification.plan import VerificationTestPlan

        verification_id = canonical_test_verification_id("graph-natural-keys")
        source_file_by_path = {
            "solutions/accepted.cpp": config.runtime_blob_store.put_bytes(
                b"int main(){return 0;}\n"
            ),
            "solutions/wa.cpp": config.runtime_blob_store.put_bytes(
                b"int main(){return 1;}\n"
            ),
        }
        graph = _build_graph(
            verification_id=verification_id,
            accepted_source_path="solutions/accepted.cpp",
            source_file_by_path=source_file_by_path,
            test_plan_by_name={
                "001.in": VerificationTestPlan(
                    test_name="001.in",
                    source_kind="gen",
                    display_source_path="generators/gen.cpp",
                    execution_source_name="gen.cpp",
                    execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
                    execution_input_file=config.runtime_blob_store.put_bytes(b"\"$SUBMISSION_BIN\"\n"),
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
                    execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
                    execution_input_file=config.runtime_blob_store.put_bytes(b"\"$SUBMISSION_BIN\"\n"),
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

    def test_custom_run_upload_preserves_source_extension_for_compile_template(
        self,
    ) -> None:
        from app.impl.workspace.verification_dag import (
            TaskExecutionContext,
            _build_graph,
            _execution_template,
        )

        for source_name, source_content in (
            ("foo.cpp", b"int main(){return 0;}\n"),
            ("Main.java", b"class Main { public static void main(String[] a) {} }\n"),
        ):
            with self.subTest(source_name=source_name):
                accepted_path = "solutions/accepted.cpp"
                uploaded_path = f"uploads/solution-0/{source_name}"
                accepted_file = config.runtime_blob_store.put_bytes(
                    b"int main(){return 0;}\n"
                )
                uploaded_file = config.runtime_blob_store.put_bytes(
                    source_content
                )
                graph = _build_graph(
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
                )
                observed: dict[str, str] = {}

                def _prepare_template(**kwargs: object) -> dict[str, object]:
                    observed["source_name"] = str(
                        kwargs.get("upload_filename") or ""
                    )
                    return {}

                with patch.object(
                    config.judgehost_task_service,
                    "prepare_execution_template",
                    side_effect=_prepare_template,
                ):
                    _execution_template(execution, program=program)

                self.assertEqual(observed.get("source_name"), source_name)

    def test_activation_installs_one_immutable_graph(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-once:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                _verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        first = config.verification_service.activate_verification(plan)
        duplicate = config.verification_service.activate_verification(plan)

        self.assertEqual(first.outcome, "activated")
        self.assertEqual(duplicate.outcome, "already-running")
        rows = config.verification_task_store.list_rows(verification_id)
        self.assertEqual([str(row["id"]) for row in rows], [task_id])

    def test_natural_task_id_accepts_longest_legal_test_name(self) -> None:
        from app.service.run.runtime import RUN_TEST_NAME_RE

        test_name = "a" + ("b" * 127) + ".in"
        self.assertEqual(len(test_name), 131)
        self.assertIsNotNone(RUN_TEST_NAME_RE.fullmatch(test_name))

        task_id = verification_task_id(
            canonical_test_verification_id("longest-test-name"),
            "accepted",
            test_name,
        )

        self.assertTrue(task_id.endswith(f"~accepted~{test_name}"))

    def test_activation_rejects_task_identity_mismatch_before_writing(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-identity:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        wrong_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                _verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=wrong_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="002.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not match its plan identity"):
            config.verification_service.activate_verification(plan)

        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(config.verification_task_store.list_rows(verification_id), [])

    def test_activation_rejects_inconsistent_program_membership(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-program:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        accepted_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        solution_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                _verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
                _verification_program(
                    program_id="solution-0",
                    kind="solution-run",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=accepted_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_id,
                    predecessor_task_id=accepted_id,
                    task_kind="solution-run",
                    source_path="solutions/b.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not match its program"):
            config.verification_service.activate_verification(plan)

        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(config.verification_task_store.list_rows(verification_id), [])

    def test_activation_rolls_back_parent_detail_and_graph(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-rollback:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={
                "mode": "interactive",
                "selected_test_names": ["001.in"],
            },
            programs=(
                _verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )
        install_activation_task_abort_fault()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced activation task failure",
            ):
                config.verification_service.activate_verification(plan)
        finally:
            clear_activation_task_abort_fault()

        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(config.verification_task_store.list_rows(verification_id), [])
        self.assertEqual(
            config.verification_service.verification_detail(verification_id)[
                "selected_test_names"
            ],
            [],
        )

    def test_activation_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                _verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _activate() -> None:
            try:
                barrier.wait()
                outcomes["activate"] = (
                    config.verification_service.activate_verification(plan).outcome
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = config.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_activate),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(outcomes["cancel"], "transitioned")
        self.assertIn(outcomes["activate"], {"activated", "closed"})
        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "failed")
        task_rows = config.verification_task_store.list_rows(verification_id)
        if outcomes["activate"] == "activated":
            self.assertEqual(len(task_rows), 1)
            self.assertEqual(
                str(task_rows[0]["status"]),
                VerificationTaskStore.TASK_CANCELLED,
            )
        else:
            self.assertEqual(task_rows, [])

    def test_completion_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                }
            ],
            edges=[],
            detail={"mode": "pass-fail", "sanity_status": ""},
        )
        completion = TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="run-completion-cancel",
            judgehost_task_id="judgehost-completion-cancel",
            result=normalize_execution_result(verdict="OK"),
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _complete() -> None:
            try:
                barrier.wait()
                commit = config.verification_task_completion_service.commit(
                    (completion,)
                )
                outcomes["completion"] = (
                    "committed"
                    if task_id in commit.committed_task_ids
                    else "already-terminal"
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = config.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_complete),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        parent = config.verification_service.verification_record(verification_id)
        assert parent is not None
        task = config.verification_task_store.list_rows(verification_id)[0]
        if str(parent["status"]) == "ok":
            self.assertEqual(outcomes, {
                "completion": "committed",
                "cancel": "closed",
            })
            self.assertEqual(task["status"], VerificationTaskStore.TASK_DONE)
        else:
            self.assertEqual(str(parent["status"]), "failed")
            self.assertEqual(outcomes, {
                "completion": "already-terminal",
                "cancel": "transitioned",
            })
            self.assertEqual(
                task["status"],
                VerificationTaskStore.TASK_CANCELLED,
            )
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStore.TASK_DONE,
                    VerificationTaskStore.TASK_FAILED,
                    VerificationTaskStore.TASK_CANCELLED,
                }
                for row in config.verification_task_store.list_rows(
                    verification_id
                )
            )
        )

    def test_finish_sanity_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"sanity-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                }
            ],
            edges=[],
            detail={"mode": "pass-fail", "sanity_status": "pending"},
        )
        completion = config.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="run-sanity-cancel",
                    judgehost_task_id="judgehost-sanity-cancel",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )
        self.assertTrue(completion.sanity_claimed)
        self.assertEqual(completion.parent_transition, "sanity-running")
        finish = SanityFinish.build(
            verification_id,
            detail={
                "mode": "pass-fail",
                "sanity_status": "passed",
                "sanity_checked_count": 1,
            },
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _finish() -> None:
            try:
                barrier.wait()
                outcomes["finish"] = config.verification_service.finish_sanity(
                    finish
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = config.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_finish),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        parent = config.verification_service.verification_record(verification_id)
        assert parent is not None
        if str(parent["status"]) == "ok":
            self.assertEqual(outcomes, {
                "finish": "transitioned",
                "cancel": "closed",
            })
            self.assertEqual(str(parent["sanity_status"]), "passed")
        else:
            self.assertEqual(str(parent["status"]), "failed")
            self.assertEqual(outcomes, {
                "finish": "closed",
                "cancel": "transitioned",
            })
            self.assertEqual(str(parent["sanity_status"]), "skipped")
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStore.TASK_DONE,
                    VerificationTaskStore.TASK_FAILED,
                    VerificationTaskStore.TASK_CANCELLED,
                }
                for row in config.verification_task_store.list_rows(
                    verification_id
                )
            )
        )

    def test_verification_lifecycle_rows_satisfy_aggregate_invariants(self) -> None:
        queued_id = canonical_test_verification_id(
            f"invariant-queued:{self.test_id}"
        )
        running_id = canonical_test_verification_id(
            f"invariant-running:{self.test_id}"
        )
        ok_id = canonical_test_verification_id(f"invariant-ok:{self.test_id}")
        failed_id = canonical_test_verification_id(
            f"invariant-failed:{self.test_id}"
        )
        for verification_id in (queued_id, running_id, ok_id, failed_id):
            self._insert_verification_row(verification_id)

        def _accepted_task(verification_id: str) -> dict[str, object]:
            return {
                "id": verification_task_id(
                    verification_id,
                    "accepted",
                    "001.in",
                ),
                "task_kind": "main-correct",
                "source_path": "solutions/accepted.cpp",
                "program_id": "accepted",
                "test_name": "001.in",
                "expected_behavior": "accepted",
            }

        running_task = _accepted_task(running_id)
        ok_task = _accepted_task(ok_id)
        self._activate_graph(
            running_id,
            tasks=[running_task],
            edges=[],
        )
        self._activate_graph(ok_id, tasks=[ok_task], edges=[])
        config.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=str(ok_task["id"]),
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="run-invariant-ok",
                    judgehost_task_id="judgehost-invariant-ok",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )
        failed = config.verification_service.cancel_verification(
            failed_id,
            reason="invariant terminal fixture",
        )
        self.assertEqual(failed.outcome, "transitioned")

        violations = db_fetch_all(
            """
            SELECT id,'queued-has-tasks' AS violation
            FROM verifications verification
            WHERE status='queued'
              AND EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'running-without-graph'
            FROM verifications verification
            WHERE status='running'
              AND NOT EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'terminal-has-open-task'
            FROM verifications verification
            WHERE status IN ('ok','failed')
              AND EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
                    AND task.final_status=''
              )
            UNION ALL
            SELECT id,'ok-without-graph'
            FROM verifications verification
            WHERE status='ok'
              AND NOT EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'ok-with-active-sanity'
            FROM verifications
            WHERE status='ok' AND sanity_status IN ('pending','running')
            """
        )
        self.assertEqual(
            [(str(row["id"]), str(row["violation"])) for row in violations],
            [],
        )



    def test_prepare_generate_input_validator_rejection_sets_failure_reason(self) -> None:
        output_file = config.runtime_blob_store.put_bytes(b"bad-input\n")
        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-validator-reject",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "program_id": "generator-0",
        }
        execution_result = _execution_result(
            verdict="WA",
            output_ref=str(output_file.blob_ref),
            feedback="validator rejected generated input\nline 2 detail",
        )
        final_result = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-generate",
                verification_id="ver-validator-reject",
                run_id="r-generate",
                result=execution_result,
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "WA")
        self.assertEqual(
            final_result.fail_reason,
            "generate-input / generators/gen.cpp / 001.in: validator rejected generated input\nline 2 detail",
        )
        self.assertEqual(final_result.error_text, "validator rejected generated input\nline 2 detail")
        self.assertEqual(final_result.feedback_text, "validator rejected generated input\nline 2 detail")
        self.assertEqual(final_result.output_ref, output_file.blob_ref)

    def test_prepare_generate_input_truncation_does_not_set_input_ref(self) -> None:
        output_file = config.runtime_blob_store.put_bytes(
            b"50000 50000\n[output storage truncated after 65536 B]\n"
        )

        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-truncated-generate",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "020.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "program_id": "generator-0",
        }
        execution_result = _execution_result(
            verdict="OK",
            output_ref=str(output_file.blob_ref),
            feedback="validator accepted",
        )
        final_result = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-generate",
                verification_id="ver-truncated-generate",
                run_id="r-generate",
                result=execution_result,
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "FL")
        self.assertEqual(final_result.error_text, "generated input output was truncated for 020.in")
        self.assertEqual(final_result.feedback_text, "generated input output was truncated for 020.in")
        self.assertEqual(final_result.output_ref, output_file.blob_ref)
        self.assertEqual(
            final_result.fail_reason,
            "generate-input / generators/gen.cpp / 020.in: generated input output was truncated for 020.in",
        )
        self.assertEqual(final_result.input_ref, "")

    def test_prepare_main_correct_preserves_canonical_compile_failure(self) -> None:
        task_row = {
            "id": "vt-main-correct",
            "verification_id": "ver-main-correct",
            "task_kind": "main-correct",
            "source_path": "solutions/std.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-main-correct",
            "run_id": "r-main-correct",
            "program_id": "accepted",
        }
        detailed_error = (
            "g++: internal compiler error: File size limit exceeded signal terminated program as\n"
            "Please submit a full bug report."
        )
        execution_result = _execution_result(
            verdict="CE",
            error=detailed_error,
            compile_log=detailed_error,
            diagnostics=[{"level": "error", "message": detailed_error}],
        )
        final_result = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-main-correct",
                verification_id="ver-main-correct",
                run_id="r-main-correct",
                result=execution_result,
                status="failed",
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "CE")
        self.assertEqual(final_result.error_text, detailed_error)
        self.assertEqual(final_result.compile_log, detailed_error)
        diagnostics_rows = json.loads(final_result.diagnostics_json)
        self.assertEqual(diagnostics_rows[0]["message"], detailed_error)
        self.assertEqual(
            final_result.fail_reason,
            f"main-correct / solutions/std.cpp / 001.in: {detailed_error}",
        )

    def test_prepare_main_correct_re_is_a_hard_failure(self) -> None:
        task_row = {
            "id": "vt-main-re",
            "verification_id": "ver-main-re",
            "task_kind": "main-correct",
            "source_path": "solutions/std.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-main-re",
            "run_id": "r-main-re",
            "program_id": "accepted",
        }
        final_result = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-main-re",
                verification_id="ver-main-re",
                run_id="r-main-re",
                result=_execution_result(
                    verdict="RE",
                    error="accepted solution crashed",
                ),
                status="ok",
                summary={"tests": [{"verdict": "RE"}]},
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "RE")
        self.assertIn("accepted solution crashed", final_result.fail_reason)

    def test_expected_compile_error_is_a_complete_solution_decision(self) -> None:
        task_row = {
            "id": "vt-solution-ce",
            "verification_id": "ver-solution-ce",
            "task_kind": "solution-run",
            "source_path": "solutions/expected-ce.cpp",
            "test_name": "001.in",
            "expected_behavior": "rejected",
            "judgehost_task_id": "jt-solution-ce",
            "run_id": "r-solution-ce",
            "program_id": "solution-0",
        }
        compile_error = _execution_result(
            verdict="CE",
            error="compiler rejected the source",
            compile_log="compiler rejected the source",
        )

        completion = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-solution-ce",
                verification_id="ver-solution-ce",
                run_id="r-solution-ce",
                result=compile_error,
                status="failed",
                summary={"error": "compile_error"},
            ),
        )

        self.assertEqual(completion.status, VerificationTaskStore.TASK_DONE)
        self.assertEqual(completion.verdict, "CE")
        self.assertEqual(completion.compile_log, "compiler rejected the source")
        self.assertEqual(completion.fail_reason, "")

    def test_expected_runtime_error_is_a_complete_solution_decision(self) -> None:
        task_row = {
            "id": "vt-solution-re",
            "verification_id": "ver-solution-re",
            "task_kind": "solution-run",
            "source_path": "solutions/expected-re.cpp",
            "test_name": "001.in",
            "expected_behavior": "run_time_error",
            "judgehost_task_id": "jt-solution-re",
            "run_id": "r-solution-re",
            "program_id": "solution-0",
        }
        runtime_error = _execution_result(
            verdict="RE",
            error="process exited with status 1",
        )

        completion = config.verification_task_completion_service.prepare(
            task_row,
            _terminal_report(
                judgehost_task_id="jt-solution-re",
                verification_id="ver-solution-re",
                run_id="r-solution-re",
                result=runtime_error,
                status="failed",
                summary={"tests": [{"verdict": "RE"}]},
            ),
        )

        self.assertEqual(completion.status, VerificationTaskStore.TASK_DONE)
        self.assertEqual(completion.verdict, "RE")
        self.assertEqual(completion.error_text, "process exited with status 1")
        self.assertEqual(completion.fail_reason, "")

    def test_solution_mismatch_waits_for_graph_then_fails_parent(self) -> None:
        verification_id = canonical_test_verification_id(
            f"solution-mismatch:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        accepted_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        rejected_id = verification_task_id(
            verification_id,
            "solution-1",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": accepted_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/expected-ac.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                },
                {
                    "id": rejected_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/expected-wa.cpp",
                    "program_id": "solution-1",
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                },
            ],
            edges=[],
        )
        task_store = config.verification_task_store
        for task_id, run_id, judgehost_task_id in (
            (accepted_id, "r-expected-ac", "jt-expected-ac"),
            (rejected_id, "r-expected-wa", "jt-expected-wa"),
        ):
            self.assertTrue(
                task_store.bind_and_expose_judgehost_runtime(
                    task_id,
                    run_id=run_id,
                    judgehost_task_id=judgehost_task_id,
                    expose=lambda: None,
                )
            )
        wa_summary = {"tests": [{"verdict": "WA"}]}
        mismatch_row = task_store.runtime_row(accepted_id)
        assert mismatch_row is not None
        mismatch = config.verification_task_completion_service.prepare(
            mismatch_row,
            _terminal_report(
                judgehost_task_id="jt-expected-ac",
                verification_id=verification_id,
                run_id="r-expected-ac",
                result=_execution_result(verdict="WA"),
                summary=wa_summary,
            ),
        )
        first_commit = config.verification_task_completion_service.commit(
            (mismatch,),
            notify=False,
        )
        self.assertEqual(mismatch.status, VerificationTaskStore.TASK_FAILED)
        self.assertIn("required=[AC]", mismatch.fail_reason)
        self.assertEqual(first_commit.parent_transition, "")
        parent = config.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "running")
        self.assertEqual(str(parent["fail_reason"]), mismatch.fail_reason)

        matched_row = task_store.runtime_row(rejected_id)
        assert matched_row is not None
        matched = config.verification_task_completion_service.prepare(
            matched_row,
            _terminal_report(
                judgehost_task_id="jt-expected-wa",
                verification_id=verification_id,
                run_id="r-expected-wa",
                result=_execution_result(verdict="WA"),
                summary=wa_summary,
            ),
        )
        final_commit = config.verification_task_completion_service.commit(
            (matched,),
            notify=False,
        )
        self.assertEqual(matched.status, VerificationTaskStore.TASK_DONE)
        self.assertEqual(matched.fail_reason, "")
        self.assertEqual(final_commit.parent_transition, "failed")
        parent = config.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "failed")
        self.assertEqual(str(parent["fail_reason"]), mismatch.fail_reason)



    def test_cancel_terminalizes_leased_and_pending_tasks(self) -> None:
        verification_id = canonical_test_verification_id("cancel")
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        running_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        pending_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": running_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        transition = config.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(transition.outcome, "transitioned")
        retry = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=running_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="r-a",
                    judgehost_task_id="jt-a",
                    result=_execution_result(verdict="AC"),
                ),
            )
        )
        rows = {
            str(row["id"]): row
            for row in task_store.list_rows(verification_id)
        }
        self.assertEqual(retry.already_terminal_task_ids, frozenset({running_id}))
        self.assertEqual(
            str(rows[running_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )

    def test_verification_summary_from_tasks_marks_cancelled_terminal_failed(self) -> None:
        from app.impl.workspace.verification_dag import _verification_summary_from_tasks

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
                "status": VerificationTaskStore.TASK_CANCELLED,
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
        status, summary, counts = _verification_summary_from_tasks(
            verification_id="ver-cancel-summary",
            artifact_verification_id="ver-cancel-summary",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                _verification_program(
                    program_id="solution-0",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                    kind="solution-run",
                )
            ],
            rows=rows,
            test_names=["001.in"],
            fail_flag=False,
            fail_reason="",
        )
        self.assertEqual(status, "failed")
        self.assertEqual(str(summary["status"]), "failed")
        self.assertEqual(int(counts["cancelled"]), 1)

    def test_task_store_caps_frontend_display_fields(self) -> None:
        verification_id = canonical_test_verification_id(
            f"display-cap:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        oversized = "x" * 5000
        diagnostics_json = json.dumps([{"level": "error", "message": "y" * 5000}], separators=(",", ":"))
        task_store.commit_task_completions((TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStore.TASK_FAILED,
            run_id="r-cap",
            judgehost_task_id="jt-cap",
            result=normalize_execution_result(
                verdict="CE",
                answer_correct=True,
                error=oversized,
                feedback=oversized,
                compile_log=oversized,
                compile_diagnostics=json.loads(diagnostics_json),
            ),
            fail_reason=oversized,
        ),))
        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertTrue(bool(row["answer_correct"]))
        limit = int(getattr(config.constants, "AUX_DISPLAY_TEXT_LIMIT_BYTES", 2048) or 2048)
        for key in ("compile_log", "error_text", "feedback_text"):
            value = str(row[key] or "")
            self.assertLessEqual(len(value.encode("utf-8")), limit)
            self.assertTrue(value.endswith("..."))
        diagnostics_rows = json.loads(str(row["diagnostics_json"] or "[]"))
        self.assertEqual(len(diagnostics_rows), 1)
        self.assertTrue(bool(diagnostics_rows[0].get("message_truncated")))
        self.assertLessEqual(len(str(diagnostics_rows[0].get("message") or "").encode("utf-8")), limit)

    def test_task_store_deduplicates_generated_content_and_skips_descendants(self) -> None:
        verification_id = canonical_test_verification_id(
            f"generated-dedup:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        owner_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        duplicate_id = verification_task_id(
            verification_id,
            "generator-0",
            "002.in",
        )
        main_id = verification_task_id(
            verification_id,
            "accepted",
            "002.in",
        )
        solution_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": owner_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": duplicate_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": solution_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[
                (duplicate_id, main_id),
                (main_id, solution_id),
            ],
        )
        task_store.commit_task_completions((TaskCompletion(
            task_id=owner_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-owner",
            judgehost_task_id="jt-owner",
            result=_execution_result(
                verdict="OK",
                output_ref="blob://same-generated-input",
            ),
            input_ref="blob://same-generated-input",
        ),))
        task_store.commit_task_completions((TaskCompletion(
            task_id=duplicate_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-duplicate",
            judgehost_task_id="jt-duplicate",
            result=_execution_result(
                verdict="OK",
                output_ref="blob://same-generated-input",
            ),
            input_ref="blob://same-generated-input",
        ),))
        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(str(rows[owner_id]["verdict"]), "OK")
        self.assertEqual(str(rows[duplicate_id]["verdict"]), "SK")
        self.assertIn(
            "duplicate generated input; skipped",
            str(rows[duplicate_id]["feedback_text"]),
        )
        for task_id in (main_id, solution_id):
            self.assertEqual(str(rows[task_id]["status"]), VerificationTaskStore.TASK_DONE)
            self.assertEqual(str(rows[task_id]["verdict"]), "SK")
            self.assertEqual(
                str(rows[task_id]["feedback_text"]),
                "skipped because generate-input was skipped",
            )

    def test_completion_commit_persists_refs_failure_and_full_result_together(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-evidence:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        generate_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        main_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": generate_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        input_file = config.runtime_blob_store.put_bytes(b"generated input\n")
        answer_file = config.runtime_blob_store.put_bytes(b"correct answer\n")
        main_result = _multi_pass_result(str(answer_file.blob_ref))

        commit = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=generate_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="r-generate-evidence",
                    judgehost_task_id="jt-generate-evidence",
                    result=_execution_result(
                        verdict="OK",
                        output_ref=str(input_file.blob_ref),
                    ),
                    input_ref=str(input_file.blob_ref),
                ),
                TaskCompletion(
                    task_id=main_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="r-main-evidence",
                    judgehost_task_id="jt-main-evidence",
                    result=main_result,
                    answer_ref=str(answer_file.blob_ref),
                    fail_reason="first durable failure context",
                ),
            )
        )

        self.assertEqual(
            commit.committed_task_ids,
            frozenset({generate_id, main_id}),
        )
        refs = config.verification_service.verification_artifact_refs(
            verification_id
        )["001.in"]
        self.assertEqual(refs["input_ref"], input_file.blob_ref)
        self.assertEqual(refs["answer_ref"], answer_file.blob_ref)
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first durable failure context",
        )
        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if row["id"] == main_id
        )
        persisted_result = row["result"]
        self.assertEqual(persisted_result.passes, main_result.passes)
        self.assertEqual(persisted_result.compile.log, main_result.compile.log)
        self.assertEqual(
            [item["message"] for item in persisted_result.compile.diagnostics],
            [item["message"] for item in main_result.compile.diagnostics],
        )
        self.assertEqual(persisted_result.warnings, main_result.warnings)
        self.assertEqual(persisted_result.outcome.usage, main_result.outcome.usage)

    def test_completion_commit_rolls_back_task_refs_failure_and_memory_state(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-rollback:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        output_file = config.runtime_blob_store.put_bytes(b"generated input\n")
        install_completion_ref_abort_fault()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced artifact ref failure",
            ):
                task_store.commit_task_completions(
                    (
                        TaskCompletion(
                            task_id=task_id,
                            status=VerificationTaskStore.TASK_FAILED,
                            run_id="r-rollback",
                            judgehost_task_id="jt-rollback",
                            result=_execution_result(
                                verdict="FL",
                                output_ref=str(output_file.blob_ref),
                                error="completion failed",
                            ),
                            input_ref=str(output_file.blob_ref),
                            fail_reason="completion failed",
                        ),
                    )
                )
        finally:
            clear_completion_ref_abort_fault()

        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStore.TASK_PENDING)
        self.assertEqual(row["result"].verdict, "")
        self.assertEqual(
            config.verification_service.verification_artifact_refs(verification_id),
            {},
        )
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["fail_reason"] or ""), "")

    def test_conflicting_completion_keeps_first_terminal_state_and_side_effects(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-first-wins:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        first = TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-first",
            judgehost_task_id="jt-first",
            result=_execution_result(
                verdict="OK",
                output_ref="blob://first-output",
                feedback="first result",
            ),
            input_ref="blob://first-output",
            fail_reason="first failure context",
        )
        task_store.commit_task_completions((first,))
        retry = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_FAILED,
                    run_id="r-conflict",
                    judgehost_task_id="jt-conflict",
                    result=_execution_result(
                        verdict="FL",
                        output_ref="blob://conflicting-output",
                        feedback="conflicting result",
                    ),
                    input_ref="blob://conflicting-output",
                    fail_reason="conflicting failure context",
                ),
            )
        )

        self.assertEqual(retry.committed_task_ids, frozenset())
        self.assertEqual(
            retry.already_terminal_task_ids,
            frozenset({task_id}),
        )
        self.assertEqual(retry.effective_completions[0].verdict, "OK")
        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStore.TASK_DONE)
        self.assertEqual(row["verdict"], "OK")
        refs = config.verification_service.verification_artifact_refs(
            verification_id
        )["001.in"]
        self.assertEqual(refs["input_ref"], "blob://first-output")
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first failure context",
        )

    def test_late_diagnostic_preserves_terminal_evidence_and_failure(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-diagnostic:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        first_failure_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        diagnostic_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": first_failure_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": diagnostic_task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        self.assertTrue(
            task_store.bind_and_expose_judgehost_runtime(
                diagnostic_task_id,
                run_id="r-amended",
                judgehost_task_id="jt-amended",
                expose=lambda: None,
            )
        )
        output_file = config.runtime_blob_store.put_bytes(b"generated input\n")
        original_result = _execution_result(
            verdict="OK",
            output_ref=str(output_file.blob_ref),
            feedback="original feedback",
            compile_log="compile evidence",
            diagnostics=[{"level": "warning", "message": "kept"}],
        )
        task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=first_failure_id,
                    status=VerificationTaskStore.TASK_FAILED,
                    run_id="r-first-failure",
                    judgehost_task_id="jt-first-failure",
                    result=_execution_result(
                        verdict="FL",
                        error="first task failed",
                    ),
                    fail_reason=(
                        "main-correct / solutions/accepted.cpp / 001.in: "
                        "first task failed"
                    ),
                ),
                TaskCompletion(
                    task_id=diagnostic_task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="r-amended",
                    judgehost_task_id="jt-amended",
                    result=original_result,
                    input_ref=str(output_file.blob_ref),
                ),
            )
        )

        outcome = task_store.append_diagnostic(
            task_id=diagnostic_task_id,
            kind="debug-info",
            hostname="judgehost-1",
            text="late debug detail",
            received_at="2026-08-10T00:00:00+00:00",
        )

        self.assertEqual(outcome, "persisted")
        rows = {
            str(row["id"]): row
            for row in task_store.list_rows(verification_id)
        }
        persisted = rows[diagnostic_task_id]
        self.assertEqual(persisted["status"], VerificationTaskStore.TASK_DONE)
        self.assertEqual(persisted["verdict"], "OK")
        self.assertEqual(persisted["result"].passes, original_result.passes)
        self.assertEqual(
            persisted["result"].compile.log,
            original_result.compile.log,
        )
        self.assertEqual(
            [
                item["message"]
                for item in persisted["result"].compile.diagnostics
            ],
            [item["message"] for item in original_result.compile.diagnostics],
        )
        self.assertEqual(persisted["result"].warnings, original_result.warnings)
        refs = config.verification_service.verification_artifact_refs(
            verification_id
        )["002.in"]
        self.assertEqual(refs["input_ref"], output_file.blob_ref)
        diagnostic = task_store.diagnostic_snapshot(diagnostic_task_id)
        self.assertEqual(len(diagnostic.items), 1)
        self.assertEqual(diagnostic.items[0].text, "late debug detail")
        self.assertEqual(
            task_store.append_diagnostic(
                task_id=diagnostic_task_id,
                kind="debug-info",
                hostname="judgehost-1",
                text="late debug detail",
                received_at="2026-08-10T00:00:01+00:00",
            ),
            "duplicate",
        )
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "main-correct / solutions/accepted.cpp / 001.in: first task failed",
        )

    def test_cancel_persists_first_failure_and_terminalizes_task(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-cancel-reason:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = config.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        transition = config.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(transition.outcome, "transitioned")
        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(
            str(row["fail_reason"] or ""),
            "verification cancelled by user",
        )
        task_row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(
            str(task_row["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )

    def test_startup_recovery_terminalizes_running_graph(self) -> None:
        verification_id = canonical_test_verification_id("startup-reconcile")
        self._insert_verification_row(verification_id)
        running_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        pending_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        task_store = config.verification_task_store
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": running_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
            detail={"mode": "pass-fail"},
        )

        summary = config.verification_service.recover_startup(
            reason="cancelled on service startup"
        )

        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(summary.verification_ids, (verification_id,))
        self.assertEqual(
            str(rows[running_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")

    def test_startup_recovery_fails_queued_verification_without_graph(self) -> None:
        verification_id = canonical_test_verification_id("startup-queued")
        self._insert_verification_row(verification_id)

        summary = config.verification_service.recover_startup(
            reason="cancelled on service startup"
        )

        self.assertEqual(summary.verification_ids, (verification_id,))
        self.assertEqual(summary.cancelled_task_ids, ())
        verification_row = config.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"] or ""),
            "cancelled on service startup",
        )
        self.assertTrue(str(verification_row["finished_at"] or ""))

    def test_verification_summary_from_tasks_excludes_main_correct_runs_from_solution_columns(self) -> None:
        from app.impl.workspace.verification_dag import _verification_summary_from_tasks

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
                "status": VerificationTaskStore.TASK_DONE,
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
                    "status": VerificationTaskStore.TASK_LEASED,
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
        status, summary, counts = _verification_summary_from_tasks(
            verification_id="ver-graph-summary",
            artifact_verification_id="ver-artifact-summary",
            mode="pass-fail",
            pass_limit=1,
            programs=[
                _verification_program(
                    program_id="accepted",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                    kind="main-correct",
                ),
                _verification_program(
                    program_id="solution-0",
                    source_path="solutions/wa.cpp",
                    expected_behavior="wrong_answer",
                    kind="solution-run",
                ),
            ],
            rows=rows,
            test_names=["001.in"],
            fail_flag=False,
            fail_reason="",
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

    def test_failure_transition_preserves_first_reason(self) -> None:
        verification_id = canonical_test_verification_id(
            f"task-store:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        first = config.verification_service.fail_verification(
            verification_id,
            reason=(
                "generate-input / generators/gen.cpp / 001.in: "
                "validator failed"
            ),
        )
        second = config.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(first.outcome, "transitioned")
        self.assertEqual(second.outcome, "closed")
        row = config.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(
            str(row["fail_reason"]),
            "generate-input / generators/gen.cpp / 001.in: validator failed",
        )
