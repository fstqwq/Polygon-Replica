from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

# tests.common installs isolated /tmp paths before app modules can create runtime config.
from .common import SmokeBase, config

from app.service.disk.verification_store import VerificationStore
from app.service.verification.task_metadata import canonical_diagnostics, canonical_truncated_text, diagnostics_json_text
from app.service.verification.task_scheduler import (
    TaskExecutionResult,
    TaskPublishResult,
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
    _ready_rows,
)
from app.service.verification.task_store import VerificationTaskStore
from app.impl.workspace.verification_dag_plan import VerificationTestPlan


def _task_row(
    task_id: str,
    *,
    task_kind: str,
    status: str,
    queue_index: int,
    source_path: str = "solutions/a.cpp",
    logical_run_id: str = "",
    test_name: str = "001.in",
) -> dict[str, object]:
    return {
        "id": task_id,
        "task_kind": task_kind,
        "source_path": source_path,
        "logical_run_id": logical_run_id,
        "test_name": test_name,
        "expected_behavior": "accepted",
        "queue_index": queue_index,
        "status": status,
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
        execution_source_bytes=b"int main(){return 0;}\n",
        execution_input_bytes=b"1\n",
        extra_sources_b64={},
        tests_meta={},
        sample=sample,
        sample_input_custom=False,
        sample_input_text="",
        uses_custom_sample_input=False,
        sample_output_text=sample_output_text,
        sample_output_validate=sample_output_validate,
    )


class _InMemoryTaskStore:
    def __init__(self, rows: list[dict[str, object]], edges: list[tuple[str, str]]) -> None:
        self._rows = [dict(row) for row in rows]
        self._edges = [{"parent_task_id": parent, "child_task_id": child} for parent, child in edges]
        self._fail_flag = False
        self._fail_reason = ""
        self._lock = threading.Lock()

    def list_rows(self, verification_id: str) -> list[dict[str, object]]:
        with self._lock:
            return [dict(row) for row in sorted(self._rows, key=lambda item: (int(item["queue_index"]), str(item["id"])))]

    def set_task_queued(self, task_id: str, *, run_id: str, judgehost_task_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["id"]) == task_id and str(row["status"]) == VerificationTaskStore.TASK_PENDING:
                    row["status"] = VerificationTaskStore.TASK_QUEUED
                    row["run_id"] = run_id
                    row["judgehost_task_id"] = judgehost_task_id
                    return

    def set_task_leased(self, task_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["id"]) == task_id and str(row["status"]) == VerificationTaskStore.TASK_QUEUED:
                    row["status"] = VerificationTaskStore.TASK_LEASED
                    return

    def save_task_result(
        self,
        task_id: str,
        *,
        status: str,
        verdict: str,
        run_id: str,
        judgehost_task_id: str,
        runtime_sec: float | None,
        cpu_sec: float | None,
        wall_sec: float | None,
        memory_kb: int | None,
        compile_log: str,
        diagnostics_json: str,
        error_text: str,
        feedback_text: str,
        output_ref: str,
        answer_correct: bool = False,
    ) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["id"]) != task_id:
                    continue
                if str(row["status"]) == VerificationTaskStore.TASK_CANCELLED:
                    return
                row["status"] = status
                row["verdict"] = verdict
                row["run_id"] = run_id
                row["compile_log"] = compile_log
                row["diagnostics_json"] = diagnostics_json
                row["error_text"] = error_text
                row["feedback_text"] = feedback_text
                row["output_ref"] = output_ref
                row["answer_correct"] = bool(answer_correct)
                return

    def cancel_unfinished_tasks(self, verification_id: str, *, reason: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["status"]) in {
                    VerificationTaskStore.TASK_PENDING,
                    VerificationTaskStore.TASK_QUEUED,
                    VerificationTaskStore.TASK_LEASED,
                }:
                    row["status"] = VerificationTaskStore.TASK_CANCELLED
                    row["cancel_reason"] = reason

    def cancel_not_started_tasks(self, verification_id: str, *, reason: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["status"]) in {
                    VerificationTaskStore.TASK_PENDING,
                    VerificationTaskStore.TASK_QUEUED,
                }:
                    row["status"] = VerificationTaskStore.TASK_CANCELLED
                    row["cancel_reason"] = reason

    def fail_state(self, verification_id: str) -> tuple[bool, str]:
        return (self._fail_flag, self._fail_reason)

    def set_fail_flag(self, verification_id: str, *, reason: str) -> None:
        if self._fail_flag:
            return
        self._fail_flag = True
        self._fail_reason = reason

    def find_runtime_row_by_judgehost_case(self, judgehost_task_id: str, test_name: str) -> dict[str, object] | None:
        with self._lock:
            for row in self._rows:
                if str(row.get("judgehost_task_id") or "") != judgehost_task_id:
                    continue
                if str(row.get("test_name") or "") != test_name:
                    continue
                return dict(row)
        return None

    def find_runtime_rows_by_judgehost_task_id(self, judgehost_task_id: str) -> list[dict[str, object]]:
        with self._lock:
            matched = [
                dict(row)
                for row in self._rows
                if str(row.get("judgehost_task_id") or "") == judgehost_task_id
            ]
        return sorted(matched, key=lambda item: (int(item["queue_index"]), str(item["id"])))


class TestVerificationTaskScheduler(SmokeBase):
    def _wait_until(self, predicate, *, timeout: float, interval: float, message: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        self.fail(message)

    def _insert_verification_row(self, verification_id: str) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
            detail={"status": "running"},
        )

    def test_required_verification_blob_waits_for_late_artifact_visibility(self) -> None:
        from app.impl.workspace.verification_dag import _verification_required_blob

        calls = {"ref": 0, "blob": 0}

        def _late_ref(_verification_id: str, _test_name: str, _ref_key: str) -> str:
            calls["ref"] += 1
            return "cache://late-input" if calls["ref"] >= 2 else ""

        def _late_blob(_ref: str) -> bytes | None:
            calls["blob"] += 1
            return b"generated\n" if calls["blob"] >= 2 else None

        with patch.object(config.verification_service, "verification_artifact_ref", side_effect=_late_ref), patch.object(
            config.verification_service,
            "resolve_artifact_blob",
            side_effect=_late_blob,
        ):
            self.assertEqual(
                _verification_required_blob(
                    "ver-late",
                    "026.in",
                    "input_ref",
                    label="verification test 026.in",
                    timeout_sec=0.2,
                    interval_sec=0.001,
                ),
                b"generated\n",
            )

        self.assertGreaterEqual(calls["ref"], 3)
        self.assertGreaterEqual(calls["blob"], 2)

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

    def test_task_publish_forwards_force_recompile_to_judgehost(self) -> None:
        from app.impl.workspace.verification_dag import (
            TASK_GENERATE_INPUT,
            TASK_MAIN_CORRECT,
            TaskExecutionContext,
            _publish_generate_task,
            _publish_run_task,
        )

        verification_id = self.random_id("ver-force-recompile")
        layout = config.fs_manager.prepare_verification_runtime_layout(verification_id)
        source_path = layout.root / "std.cpp"
        source_path.write_text("int main(){return 0;}\n", encoding="utf-8")
        execution = TaskExecutionContext(
            problem=self.problem,
            user=self.user,
            verification_id=verification_id,
            mode="pass-fail",
            pass_limit=1,
            snapshot_root=layout.root,
            uploaded_sources_root=layout.uploaded_sources,
            source_file_by_path={"solutions/std.cpp": source_path},
            test_plan_by_name={"001.in": _sanity_test_plan()},
            run_verification_payload_base={},
            generate_verification_payload_base={},
            force_recompile=True,
        )
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            calls.append(dict(kwargs))
            return f"jt-force-{len(calls)}"

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch(
            "app.impl.workspace.verification_dag._verification_required_blob",
            return_value=b"1\n",
        ):
            _publish_generate_task(
                _task_row(
                    "vt-generate",
                    task_kind=TASK_GENERATE_INPUT,
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                ),
                execution=execution,
                test_plan=_sanity_test_plan(),
            )
            _publish_run_task(
                _task_row(
                    "vt-main",
                    task_kind=TASK_MAIN_CORRECT,
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/std.cpp",
                    logical_run_id="main",
                ),
                execution=execution,
            )

        self.assertEqual([call["force_recompile"] for call in calls], [True, True])

    def test_summary_runtime_threshold_marks_answer_correct_points(self) -> None:
        from app.impl.workspace.runtime_threshold import evaluate_summary_runtime_threshold

        report = evaluate_summary_runtime_threshold(
            summary={
                "tests": [
                    {"test": "001.in", "verdict": "OK", "time_user_ms": 600, "answer_correct": True},
                    {"test": "002.in", "verdict": "TL", "time_user_ms": 1200, "answer_correct": True},
                    {"test": "003.in", "verdict": "WA", "time_user_ms": 700, "answer_correct": False},
                ]
            },
            source="solutions/slow.cpp",
            time_limit_ms=1000,
        )
        self.assertEqual(report.highlighted_tests, frozenset({"001.in", "002.in"}))
        self.assertIsNone(report.warning_hit)

        all_correct_report = evaluate_summary_runtime_threshold(
            summary={
                "tests": [
                    {"test": "001.in", "verdict": "OK", "time_user_ms": 600, "answer_correct": True},
                    {"test": "002.in", "verdict": "OK", "time_user_ms": 1200, "answer_correct": True},
                ]
            },
            source="solutions/accepted.cpp",
            time_limit_ms=1000,
        )
        self.assertIsNotNone(all_correct_report.warning_hit)

    def test_boundary_coverage_aggregates_testlib_overview_logs(self) -> None:
        from app.impl.workspace.boundary_coverage import boundary_coverage_from_feedback

        first = (
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
        )
        second = (
            '"n": max-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
        )
        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": first, "002.in": second},
            test_plans=[_sanity_test_plan(test_name="001.in"), _sanity_test_plan(test_name="002.in")],
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.missing, [])

    def test_boundary_coverage_warns_for_missing_hits_and_respects_skipped_bounds(self) -> None:
        from app.impl.workspace.boundary_coverage import boundary_coverage_from_feedback

        feedback = (
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
            '"~T~": min-value-hit max-value-hit\n'
            'constant-bounds "~T~": 0 10\n'
            'variable "~T~"\n'
            'constant-bounds "x": ? 9\n'
            'variable "x"\n'
        )
        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": feedback},
            test_plans=[_sanity_test_plan()],
        )
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.missing, ["n max=3", "x max=9"])
        self.assertEqual(result.error, "Test data did not hit: n max=3, x max=9")

    def test_boundary_coverage_ignores_wrapped_or_plain_messages(self) -> None:
        from app.impl.workspace.boundary_coverage import boundary_coverage_from_feedback

        wrapped = (
            "__POLYGON_TESTLIB_OVERVIEW_BEGIN__\n"
            '"n": min-value-hit\n'
            'constant-bounds "n": 1 3\n'
            'variable "n"\n'
            "__POLYGON_TESTLIB_OVERVIEW_END__\n"
        )
        result = boundary_coverage_from_feedback(
            feedback_by_test={"001.in": wrapped, "002.in": "validator accepted\n"},
            test_plans=[_sanity_test_plan(test_name="001.in"), _sanity_test_plan(test_name="002.in")],
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 0)
        self.assertEqual(result.missing, [])

    def test_sanity_stability_probes_pass_on_non_ac_non_fl(self) -> None:
        from app.impl.workspace.sanity_checks import run_verification_sanity_checks

        verification_id = self.random_id("ver-sanity-stable")
        logs_dir = config.fs_manager.prepare_verification_root(verification_id).resolve() / "logs"
        calls: list[dict[str, object]] = []

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

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.checked_count, 2)
        self.assertEqual([str(call["upload_filename"]) for call in calls], ["sanity_empty_output.py", "sanity_unicode_output.py"])
        self.assertTrue(all(call["persist_verification_run"] is False for call in calls))
        self.assertTrue(all(call["selected_tests"] == ["001.in"] for call in calls))
        self.assertTrue(all(call["expected_behavior"] == "unknown" for call in calls))
        self.assertIn("empty_output_stability 001.in: ok - WA", (logs_dir / "stability.log").read_text(encoding="utf-8"))

    def test_sanity_boundary_coverage_warning_keeps_verification_ok(self) -> None:
        from app.impl.workspace.sanity_checks import BOUNDARY_COVERAGE_CHECK, run_verification_sanity_checks

        verification_id = self.random_id("ver-sanity-boundary")
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

        verification_id = self.random_id("ver-sanity-runtime")
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
            LogicalRunSpec,
            _runtime_threshold_columns_from_tasks,
        )

        def row(
            *,
            task_id: str,
            task_kind: str,
            source_path: str,
            logical_run_id: str,
        ) -> dict[str, object]:
            return {
                "id": task_id,
                "verification_id": "ver-runtime-columns",
                "predecessor_task_id": "",
                "task_kind": task_kind,
                "source_path": source_path,
                "logical_run_id": logical_run_id,
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
            logical_runs=[
                LogicalRunSpec(
                    logical_run_id="main",
                    source_path="solutions/std.cpp",
                    expected_behavior="accepted",
                    task_kind=TASK_MAIN_CORRECT,
                ),
                LogicalRunSpec(
                    logical_run_id="solution",
                    source_path="solutions/other.cpp",
                    expected_behavior="accepted",
                    task_kind=TASK_SOLUTION_RUN,
                ),
            ],
            rows=[
                row(
                    task_id="vt-main",
                    task_kind=TASK_MAIN_CORRECT,
                    source_path="solutions/std.cpp",
                    logical_run_id="main",
                ),
                row(
                    task_id="vt-solution",
                    task_kind=TASK_SOLUTION_RUN,
                    source_path="solutions/other.cpp",
                    logical_run_id="solution",
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

        verification_id = self.random_id("ver-sanity-ac")
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

        verification_id = self.random_id("ver-sanity-fl")
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
        from app.impl.workspace.verification_dag_plan import VerificationTestPlan

        class _IdStore:
            def __init__(self) -> None:
                self._next = 1

            def allocate_id(self) -> str:
                current = self._next
                self._next += 1
                return f"vt-{current}"

        graph = _build_graph(
            task_store=_IdStore(),
            accepted_source_path="solutions/accepted.cpp",
            test_plan_by_name={
                "001.in": VerificationTestPlan(
                    test_name="001.in",
                    source_kind="gen",
                    display_source_path="generators/gen.cpp",
                    execution_source_name="gen.cpp",
                    execution_source_bytes=b"int main(){return 0;}\n",
                    execution_input_bytes=b"\"$SUBMISSION_BIN\"\n",
                    extra_sources_b64={},
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
                    execution_source_bytes=b"int main(){return 0;}\n",
                    execution_input_bytes=b"\"$SUBMISSION_BIN\"\n",
                    extra_sources_b64={},
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
                    "run_id": "r-main",
                },
                {
                    "path": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "run_id": "r-wa",
                },
            ],
            test_names=["001.in", "002.in"],
        )
        generate = [row for row in graph.tasks if str(row["task_kind"]) == "generate-input"]
        main = [row for row in graph.tasks if str(row["task_kind"]) == "main-correct"]
        solution = [row for row in graph.tasks if str(row["task_kind"]) == "solution-run"]
        self.assertEqual(len(generate), 2)
        self.assertEqual(len(main), 2)
        self.assertEqual(len(solution), 2)
        self.assertEqual(len(graph.edges), 4)
        self.assertEqual({str(row["source_path"]) for row in generate}, {"generators/gen.cpp"})

    def test_runtime_coordinator_publishes_successor_after_case_report_event(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                ),
                _task_row(
                    "vt-main",
                    task_kind="main-correct",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/main.cpp",
                ),
            ],
            edges=[("vt-generate", "vt-main")],
        )
        publish_order: list[str] = []
        persist_counts: list[int] = []
        final_result = TaskExecutionResult(
            task_id="vt-generate",
            status=VerificationTaskStore.TASK_DONE,
            verdict="OK",
            run_id="r-generate",
            judgehost_task_id="jt-generate",
            runtime_sec=0.01,
            cpu_sec=0.01,
            wall_sec=0.01,
            memory_kb=1,
            compile_log="",
            diagnostics_json="[]",
            error_text="",
            feedback_text="",
            output_ref="",
        )

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )

        def _persist() -> dict[str, object]:
            persist_counts.append(len(persist_counts))
            rows = store.list_rows("ver-runtime")
            total = len(rows)
            pending = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_PENDING)
            queued = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_QUEUED)
            running = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_LEASED)
            done = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_DONE)
            failed = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_FAILED)
            cancelled = sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_CANCELLED)
            return {
                "total": total,
                "pending": pending,
                "queued": queued,
                "running": running,
                "done": done,
                "failed": failed,
                "cancelled": cancelled,
            }

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_queued_tasks=lambda _reason: None,
            persist_state=_persist,
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime",
            task_store=store,
            callbacks=callbacks,
            edges=[("vt-generate", "vt-main")],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: publish_order == ["vt-generate"],
                timeout=2.0,
                interval=0.01,
                message="initial ready task was not published",
            )
            self.assertEqual(store.list_rows("ver-runtime")[0]["status"], VerificationTaskStore.TASK_QUEUED)
            coordinator.enqueue_case_reported(
                "jt-vt-generate",
                "001.in",
                {"final_result": final_result},
            )
            self._wait_until(
                lambda: publish_order == ["vt-generate", "vt-main"],
                timeout=2.0,
                interval=0.01,
                message="successor task was not published after case result event",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-runtime")}
            self.assertEqual(str(rows["vt-generate"]["status"]), VerificationTaskStore.TASK_DONE)
            self.assertEqual(str(rows["vt-main"]["status"]), VerificationTaskStore.TASK_QUEUED)
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_publishes_ready_batch_before_case_events(self) -> None:
        total_tasks = 32
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    f"vt-{index:03}",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=index + 1,
                    source_path="generators/gen.cpp",
                    test_name=f"{index + 1:03}.in",
                )
                for index in range(total_tasks)
            ],
            edges=[],
        )
        publish_order: list[str] = []
        first_published = threading.Event()
        final_result = TaskExecutionResult(
            task_id="vt-000",
            status=VerificationTaskStore.TASK_DONE,
            verdict="OK",
            run_id="r-vt-000",
            judgehost_task_id="jt-vt-000",
            runtime_sec=0.01,
            cpu_sec=0.01,
            wall_sec=0.01,
            memory_kb=1,
            compile_log="",
            diagnostics_json="[]",
            error_text="",
            feedback_text="",
            output_ref="",
        )

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            if task_id == "vt-000":
                first_published.set()
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_queued_tasks=lambda _reason: None,
            persist_state=lambda: {},
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-large-batch",
            task_store=store,
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(first_published.wait(timeout=2.0))
            coordinator.enqueue_case_reported(
                "jt-vt-000",
                "001.in",
                {"final_result": final_result},
            )
            self._wait_until(
                lambda: len(publish_order) == total_tasks,
                timeout=2.0,
                interval=0.01,
                message="ready batch was not fully published before handling case events",
            )
            self._wait_until(
                lambda: str({str(row["id"]): row for row in store.list_rows("ver-large-batch")}["vt-000"]["status"])
                == VerificationTaskStore.TASK_DONE,
                timeout=2.0,
                interval=0.01,
                message="case result was not handled after publishing ready batch",
            )
            self.assertEqual(len(publish_order), total_tasks)
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_finalize_generate_input_validator_rejection_fails_task_and_sets_fail_flag_reason(self) -> None:
        from app.service.verification.task_result_finalize import finalize_verification_task_result

        class _JudgehostTaskService:
            def domjudge_case_output_for_task(self, judgehost_task_id: str, test_name: str) -> tuple[str, None, str]:
                self.seen = (judgehost_task_id, test_name)
                return ("", None, "")

            def resolve_artifact_blob(self, output_ref: str, *, work_root: object = None) -> bytes | None:
                self.seen_output_ref = output_ref
                return b"bad-input\n"

        fake_task_service = _JudgehostTaskService()
        fake_config = SimpleNamespace(judgehost_task_service=fake_task_service)
        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-validator-reject",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "logical_run_id": "r-generate",
        }
        result = {
            "status": "ok",
            "summary": {
                "tests": [
                    {
                        "verdict": "WA",
                        "message": "validator rejected generated input\nline 2 detail",
                        "output_ref": "cache://case/output/001.out",
                        "time_ms": 7,
                        "time_user_ms": 7,
                        "time_wall_ms": 8,
                        "memory_kb": 64,
                    }
                ]
            },
        }

        with patch("app.impl.runtime.config.config", fake_config):
            final_result = finalize_verification_task_result(task_row, result=result)

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "WA")
        self.assertEqual(
            final_result.fail_flag_reason,
            "generate-input / generators/gen.cpp / 001.in: validator rejected generated input\nline 2 detail",
        )
        self.assertEqual(final_result.error_text, "validator rejected generated input\nline 2 detail")
        self.assertEqual(final_result.feedback_text, "validator rejected generated input\nline 2 detail")
        self.assertEqual(final_result.output_ref, "cache://case/output/001.out")

    def test_finalize_generate_input_truncation_fails_before_persisting_input_ref(self) -> None:
        from app.service.verification.task_result_finalize import finalize_verification_task_result

        class _JudgehostTaskService:
            def domjudge_case_output_for_task(self, judgehost_task_id: str, test_name: str) -> tuple[str, None, str]:
                self.seen = (judgehost_task_id, test_name)
                return ("", None, "")

            def resolve_artifact_blob(self, output_ref: str, *, work_root: object = None) -> bytes | None:
                self.seen_output_ref = output_ref
                return b"50000 50000\n[output storage truncated after 65536 B]\n"

        class _VerificationService:
            def __init__(self) -> None:
                self.stored: list[dict[str, object]] = []
                self.updated: list[tuple[str, str, dict[str, str]]] = []

            def store_verification_blob(self, **kwargs: object) -> str:
                self.stored.append(dict(kwargs))
                return "cache://verification/should-not-store"

            def update_verification_artifact_refs(
                self,
                verification_id: str,
                test_name: str,
                refs: dict[str, str],
            ) -> dict[str, object]:
                self.updated.append((verification_id, test_name, dict(refs)))
                return {}

        fake_task_service = _JudgehostTaskService()
        fake_verification_service = _VerificationService()
        fake_config = SimpleNamespace(
            judgehost_task_service=fake_task_service,
            verification_service=fake_verification_service,
        )
        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-truncated-generate",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "020.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "logical_run_id": "r-generate",
        }
        result = {
            "status": "ok",
            "summary": {
                "tests": [
                    {
                        "verdict": "OK",
                        "message": "validator accepted",
                        "output_ref": "cache://case/output/020.out",
                        "time_ms": 7,
                        "time_user_ms": 7,
                        "time_wall_ms": 8,
                        "memory_kb": 64,
                    }
                ]
            },
        }

        with patch("app.impl.runtime.config.config", fake_config):
            final_result = finalize_verification_task_result(task_row, result=result)

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "FL")
        self.assertEqual(final_result.error_text, "generated input output was truncated for 020.in")
        self.assertEqual(final_result.feedback_text, "generated input output was truncated for 020.in")
        self.assertEqual(final_result.output_ref, "cache://case/output/020.out")
        self.assertEqual(
            final_result.fail_flag_reason,
            "generate-input / generators/gen.cpp / 020.in: generated input output was truncated for 020.in",
        )
        self.assertEqual(fake_verification_service.stored, [])
        self.assertEqual(fake_verification_service.updated, [])

    def test_finalize_main_correct_prefers_detailed_summary_error_over_generic_result_error(self) -> None:
        from app.service.verification.task_result_finalize import finalize_verification_task_result

        task_row = {
            "id": "vt-main-correct",
            "verification_id": "ver-main-correct",
            "task_kind": "main-correct",
            "source_path": "solutions/std.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-main-correct",
            "run_id": "r-main-correct",
            "logical_run_id": "r-main-correct",
        }
        detailed_error = (
            "g++: internal compiler error: File size limit exceeded signal terminated program as\n"
            "Please submit a full bug report."
        )
        result = {
            "status": "failed",
            "error": "compile error",
            "summary": {
                "error": detailed_error,
                "compile_diagnostics": [
                    {
                        "level": "error",
                        "message": detailed_error,
                    }
                ],
                "tests": [
                    {
                        "verdict": "CE",
                        "message": "",
                        "output_ref": "",
                        "time_ms": 0,
                        "time_user_ms": 0,
                        "time_wall_ms": 0,
                        "memory_kb": 0,
                    }
                ],
            },
        }

        final_result = finalize_verification_task_result(task_row, result=result)

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "CE")
        self.assertEqual(final_result.error_text, detailed_error)
        self.assertEqual(final_result.compile_log, detailed_error)
        diagnostics_rows = json.loads(final_result.diagnostics_json)
        self.assertEqual(diagnostics_rows[0]["message"], detailed_error)
        self.assertEqual(
            final_result.fail_flag_reason,
            f"main-correct / solutions/std.cpp / 001.in: {detailed_error}",
        )

    def test_runtime_coordinator_cancels_successors_after_generate_input_validator_rejection(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                ),
                _task_row(
                    "vt-solution",
                    task_kind="solution-run",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/ok.cpp",
                    logical_run_id="r-solution",
                ),
            ],
            edges=[("vt-generate", "vt-solution")],
        )
        publish_order: list[str] = []
        final_result = TaskExecutionResult(
            task_id="vt-generate",
            status=VerificationTaskStore.TASK_FAILED,
            verdict="WA",
            run_id="r-generate",
            judgehost_task_id="jt-vt-generate",
            runtime_sec=0.01,
            cpu_sec=0.01,
            wall_sec=0.01,
            memory_kb=1,
            compile_log="",
            diagnostics_json="[]",
            error_text="validator rejected generated input for 001.in",
            feedback_text="validator rejected generated input for 001.in",
            output_ref="cache://case/output/001.out",
            fail_flag_reason="generate-input / generators/gen.cpp / 001.in: validator rejected generated input for 001.in",
        )

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_queued_tasks=lambda _reason: None,
            persist_state=lambda: {},
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-validator-stop",
            task_store=store,
            callbacks=callbacks,
            edges=[("vt-generate", "vt-solution")],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: publish_order == ["vt-generate"],
                timeout=2.0,
                interval=0.01,
                message="generate-input task was not published",
            )
            coordinator.enqueue_case_reported(
                "jt-vt-generate",
                "001.in",
                {"final_result": final_result},
            )
            self._wait_until(
                lambda: store.fail_state("ver-validator-stop")[0],
                timeout=2.0,
                interval=0.01,
                message="validator rejection did not set fail flag",
            )
            self._wait_until(
                lambda: str({str(row["id"]): row for row in store.list_rows("ver-validator-stop")}["vt-solution"]["status"])
                == VerificationTaskStore.TASK_CANCELLED,
                timeout=2.0,
                interval=0.01,
                message="successor task was not cancelled after validator rejection",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-validator-stop")}
            self.assertEqual(str(rows["vt-generate"]["status"]), VerificationTaskStore.TASK_FAILED)
            self.assertEqual(str(rows["vt-solution"]["status"]), VerificationTaskStore.TASK_CANCELLED)
            self.assertEqual(publish_order, ["vt-generate"])
            self.assertEqual(
                store.fail_state("ver-validator-stop"),
                (True, "generate-input / generators/gen.cpp / 001.in: validator rejected generated input for 001.in"),
            )
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_idle_waits_for_events_without_polling(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                )
            ],
            edges=[],
        )
        persist_calls = 0

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            return TaskPublishResult(
                task_id=str(row["id"]),
                run_id="r-generate",
                judgehost_task_id="jt-generate",
            )

        def _persist() -> dict[str, object]:
            nonlocal persist_calls
            persist_calls += 1
            rows = store.list_rows("ver-runtime-idle")
            return {
                "total": len(rows),
                "pending": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_PENDING),
                "queued": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_QUEUED),
                "running": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_LEASED),
                "done": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_DONE),
                "failed": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_FAILED),
                "cancelled": sum(1 for row in rows if str(row["status"]) == VerificationTaskStore.TASK_CANCELLED),
            }

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_queued_tasks=lambda _reason: None,
            persist_state=_persist,
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime-idle",
            task_store=store,
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: persist_calls >= 1,
                timeout=2.0,
                interval=0.01,
                message="bootstrap persist did not run",
            )
            baseline = persist_calls
            time.sleep(0.2)
            self.assertEqual(persist_calls, baseline)
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_cancel_releases_worker_without_finalizing_leased_rows(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        "vt-leased",
                        task_kind="solution-run",
                        status=VerificationTaskStore.TASK_LEASED,
                        queue_index=1,
                        logical_run_id="r-a",
                        test_name="001.in",
                    ),
                    "run_id": "r-a",
                    "judgehost_task_id": "jt-leased",
                },
                {
                    **_task_row(
                        "vt-queued",
                        task_kind="solution-run",
                        status=VerificationTaskStore.TASK_QUEUED,
                        queue_index=2,
                        logical_run_id="r-b",
                        test_name="002.in",
                    ),
                    "run_id": "r-b",
                    "judgehost_task_id": "jt-queued",
                },
            ],
            edges=[],
        )
        queued_cancel_reasons: list[str] = []
        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda _row: (_ for _ in ()).throw(RuntimeError("unexpected publish")),
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_queued_tasks=lambda reason: queued_cancel_reasons.append(reason),
            persist_state=lambda: {},
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime-cancel",
            task_store=store,
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            coordinator.enqueue_cancel("verification cancelled by user")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            store.cancel_not_started_tasks("ver-runtime-cancel", reason="verification cancelled by user")
            self._wait_until(
                lambda: str({str(row["id"]): row for row in store.list_rows("ver-runtime-cancel")}["vt-queued"]["status"]) == VerificationTaskStore.TASK_CANCELLED,
                timeout=2.0,
                interval=0.01,
                message="queued row was not cancelled",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-runtime-cancel")}
            self.assertEqual(str(rows["vt-leased"]["status"]), VerificationTaskStore.TASK_LEASED)
            self.assertEqual(str(rows["vt-queued"]["status"]), VerificationTaskStore.TASK_CANCELLED)
            self.assertEqual(queued_cancel_reasons, [])
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())

    def test_cancel_not_started_tasks_leaves_leased_rows_reportable(self) -> None:
        self._insert_verification_row("ver-cancel")
        task_store = VerificationTaskStore(config.db)
        task_store.replace_graph(
            "ver-cancel",
            tasks=[
                {
                    "id": "vt-running",
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "logical_run_id": "r-a",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": "vt-pending",
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "logical_run_id": "r-a",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        task_store.cancel_not_started_tasks("ver-cancel", reason="verification cancelled by user")
        task_store.save_task_result(
            "vt-running",
            status=VerificationTaskStore.TASK_DONE,
            verdict="AC",
            run_id="r-a",
            judgehost_task_id="jt-a",
            runtime_sec=0.1,
            cpu_sec=0.1,
            wall_sec=0.1,
            memory_kb=1,
            compile_log="",
            diagnostics_json="[]",
            error_text="",
            feedback_text="",
            output_ref="",
        )
        rows = {str(row["id"]): row for row in task_store.list_rows("ver-cancel")}
        self.assertEqual(str(rows["vt-running"]["status"]), VerificationTaskStore.TASK_DONE)
        self.assertEqual(str(rows["vt-pending"]["status"]), VerificationTaskStore.TASK_CANCELLED)

    def test_verification_summary_from_tasks_marks_cancelled_terminal_failed(self) -> None:
        from app.impl.workspace.verification_dag import LogicalRunSpec, _verification_summary_from_tasks

        rows = [
            {
                "id": "vt-solution",
                "verification_id": "ver-cancel-summary",
                "task_kind": "solution-run",
                "source_path": "solutions/a.cpp",
                "logical_run_id": "r-a",
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
            logical_runs=[
                LogicalRunSpec(
                    logical_run_id="r-a",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                    task_kind="solution-run",
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

    def test_verification_summary_from_tasks_finishes_cancelled_fail_flag_with_leased_rows(self) -> None:
        from app.impl.workspace.verification_dag import LogicalRunSpec, _verification_summary_from_tasks

        rows = [
            {
                "id": "vt-leased",
                "verification_id": "ver-cancel-leased-summary",
                "task_kind": "solution-run",
                "source_path": "solutions/a.cpp",
                "logical_run_id": "r-a",
                "test_name": "001.in",
                "expected_behavior": "accepted",
                "queue_index": 1,
                "status": VerificationTaskStore.TASK_LEASED,
                "verdict": "",
                "run_id": "r-a",
                "judgehost_task_id": "jt-a",
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
                "finished_at": "",
                "cancel_reason": "",
                "created_at": "2026-03-23T00:00:00Z",
                "updated_at": "2026-03-23T00:00:01Z",
            }
        ]
        status, summary, counts = _verification_summary_from_tasks(
            verification_id="ver-cancel-leased-summary",
            artifact_verification_id="ver-cancel-leased-summary",
            mode="pass-fail",
            pass_limit=1,
            logical_runs=[
                LogicalRunSpec(
                    logical_run_id="r-a",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                    task_kind="solution-run",
                )
            ],
            rows=rows,
            test_names=["001.in"],
            fail_flag=True,
            fail_reason="verification cancelled by user",
        )
        self.assertEqual(status, "failed")
        self.assertEqual(str(summary["status"]), "failed")
        self.assertTrue(str(summary["finished_at"] or ""))
        self.assertEqual(int(counts["running"]), 1)

    def test_task_store_caps_frontend_display_fields(self) -> None:
        verification_id = f"ver-display-cap-{self.test_id}"
        self._insert_verification_row(verification_id)
        task_store = VerificationTaskStore(config.db)
        task_store.replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-cap",
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "logical_run_id": "r-cap",
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
        task_store.save_task_result(
            "vt-cap",
            status=VerificationTaskStore.TASK_FAILED,
            verdict="CE",
            run_id="r-cap",
            judgehost_task_id="jt-cap",
            runtime_sec=None,
            cpu_sec=None,
            wall_sec=None,
            memory_kb=None,
            compile_log=oversized,
            diagnostics_json=diagnostics_json,
            error_text=oversized,
            feedback_text=oversized,
            output_ref="",
            answer_correct=True,
        )
        row = task_store.list_rows(verification_id)[0]
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
        task_store.set_fail_flag(verification_id, reason=oversized)
        fail_flag, fail_reason = task_store.fail_state(verification_id)
        self.assertTrue(fail_flag)
        self.assertLessEqual(len(fail_reason.encode("utf-8")), limit)
        self.assertTrue(fail_reason.endswith("..."))

    def test_startup_cancel_task_graph_verifications_reconciles_stale_rows(self) -> None:
        from app.impl.auth.internal.runtime import _startup_cancel_task_graph_verifications

        verification_id = "ver-startup-reconcile"
        self._insert_verification_row(verification_id)
        config.verification_service.persist_verification_detail(
            verification_id,
            {
                "verification_id": verification_id,
                "artifact_verification_id": verification_id,
                "task_graph": True,
                "status": "failed",
                "mode": "pass-fail",
                "error": "cancelled on service startup",
            },
        )
        task_store = VerificationTaskStore(config.db)
        task_store.replace_graph(
            verification_id,
            tasks=[
                {
                    "id": "vt-running",
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "logical_run_id": "r-a",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStore.TASK_LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": "vt-pending",
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "logical_run_id": "r-a",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )

        _startup_cancel_task_graph_verifications("cancelled on service startup")

        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(str(rows["vt-running"]["status"]), VerificationTaskStore.TASK_CANCELLED)
        self.assertEqual(str(rows["vt-pending"]["status"]), VerificationTaskStore.TASK_CANCELLED)
        verification_row = VerificationStore(config.db).record_row(verification_id)
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")

    def test_startup_finalize_cancelled_verifications_fills_missing_finished_at(self) -> None:
        from app.impl.auth.internal.runtime import _startup_finalize_cancelled_verifications

        verification_id = "ver-startup-user-cancel"
        self._insert_verification_row(verification_id)
        config.verification_service.update_verification_record_status(
            verification_id,
            status="failed",
            fail_reason="verification cancelled by user",
            finished=False,
        )

        _startup_finalize_cancelled_verifications("2026-04-20T00:00:00Z")

        verification_row = VerificationStore(config.db).record_row(verification_id)
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")
        self.assertEqual(str(verification_row["fail_reason"] or ""), "verification cancelled by user")
        self.assertEqual(str(verification_row["finished_at"] or ""), "2026-04-20T00:00:00Z")

    def test_verification_summary_from_tasks_excludes_main_correct_runs_from_solution_columns(self) -> None:
        from app.impl.workspace.verification_dag import LogicalRunSpec, _verification_summary_from_tasks

        rows = [
            {
                "id": "vt-main",
                "verification_id": "ver-graph-summary",
                "task_kind": "main-correct",
                "source_path": "solutions/accepted.cpp",
                "logical_run_id": "r-main",
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
                "logical_run_id": "r-wa",
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
            logical_runs=[
                LogicalRunSpec(
                    logical_run_id="r-main",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                    task_kind="main-correct",
                ),
                LogicalRunSpec(
                    logical_run_id="r-wa",
                    source_path="solutions/wa.cpp",
                    expected_behavior="wrong_answer",
                    task_kind="solution-run",
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

    def test_summary_parts_uses_canonical_metadata_shapes(self) -> None:
        from app.service.verification.task_result_finalize import _summary_parts

        parts = _summary_parts(
            {
                "error": "compile failed\n" + ("x" * 5000),
                "compile_diagnostics": [{"level": "error", "message": "y" * 5000}],
                "tests": [
                    {
                        "verdict": "WA",
                        "message": "z" * 64,
                        "output_ref": "ref-output",
                        "time_ms": 10,
                        "time_user_ms": 8,
                        "time_wall_ms": 12,
                        "memory_kb": 123,
                    }
                ],
            },
            run_status="ok",
            error_text="compile failed",
        )
        diagnostics_rows = json.loads(parts.diagnostics_json)
        self.assertEqual(parts.verdict, "WA")
        self.assertEqual(parts.output_ref, "ref-output")
        self.assertTrue(str(parts.compile_log).startswith("compile failed"))
        self.assertTrue(bool(diagnostics_rows[0].get("message_truncated")))
        self.assertEqual(parts.memory_kb, 123)
        self.assertAlmostEqual(parts.runtime_sec or 0.0, 0.01, places=3)

    def test_summary_parts_synthesizes_error_text_for_ce_without_compile_detail(self) -> None:
        from app.service.verification.task_result_finalize import _summary_parts

        parts = _summary_parts(
            {
                "tests": [
                    {
                        "verdict": "CE",
                        "message": "",
                        "output_ref": "",
                        "time_ms": 0,
                        "time_user_ms": 0,
                        "time_wall_ms": 0,
                        "memory_kb": 0,
                    }
                ],
            },
            run_status="failed",
            error_text="",
        )
        self.assertEqual(parts.verdict, "CE")
        self.assertEqual(parts.error_text, "compile error")

    def test_ready_rows_require_all_parent_tasks_done(self) -> None:
        rows = [
            _task_row("parent-done", task_kind="generate-input", status=VerificationTaskStore.TASK_DONE, queue_index=1),
            _task_row("parent-leased", task_kind="generate-input", status=VerificationTaskStore.TASK_LEASED, queue_index=2),
            _task_row("child-ready", task_kind="main-correct", status=VerificationTaskStore.TASK_PENDING, queue_index=3),
            _task_row("child-blocked", task_kind="main-correct", status=VerificationTaskStore.TASK_PENDING, queue_index=4),
            _task_row("root-pending", task_kind="generate-input", status=VerificationTaskStore.TASK_PENDING, queue_index=5),
        ]
        ready = _ready_rows(
            rows,
            [
                ("parent-done", "child-ready"),
                ("parent-leased", "child-blocked"),
            ],
        )
        self.assertEqual([str(row["id"]) for row in ready], ["child-ready", "root-pending"])

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

    def test_task_store_persists_fail_flag(self) -> None:
        verification_id = f"ver-task-store-{self.test_id}"
        self._insert_verification_row(verification_id)
        store = VerificationTaskStore(config.db)
        store.set_fail_flag(verification_id, reason="main failed")
        self.assertEqual(store.fail_state(verification_id), (True, "main failed"))
        self.assertIsNotNone(VerificationStore(config.db).record_row(verification_id))

    def test_task_store_keeps_first_fail_flag_reason(self) -> None:
        verification_id = f"ver-task-store-first-{self.test_id}"
        self._insert_verification_row(verification_id)
        store = VerificationTaskStore(config.db)
        store.set_fail_flag(verification_id, reason="generate-input / generators/gen.cpp / 001.in: validator failed")
        store.set_fail_flag(verification_id, reason="verification cancelled by user")
        self.assertEqual(
            store.fail_state(verification_id),
            (True, "generate-input / generators/gen.cpp / 001.in: validator failed"),
        )
