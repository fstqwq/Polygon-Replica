import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

from app.service.judgehost.domjudge.wire_model import DomjudgeWork
from app.service.judgehost.ports.completion import CaseTerminalReport
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.execution import VerificationCoordinatorFailure
from app.service.verification.sanity import (
    BOUNDARY_COVERAGE_CHECK,
    EMPTY_OUTPUT_STABILITY_CHECK,
    SUMMARY_RUNTIME_THRESHOLD_CHECK,
    UNICODE_OUTPUT_STABILITY_CHECK,
)
from app.service.verification.types import VerificationTaskStatus

from tests.common import runtime
from tests.isolated_db_helpers import isolated_db_execute
from tests.identity_helpers import canonical_test_verification_id
from tests.judgehost_support import JudgehostReply, reporting_judgehost
from tests.verification_adapter_fixture import VerificationExecutionTestBase, sanity_test_plan


class TestVerificationAdapters(VerificationExecutionTestBase):
    def test_task_publication_runs_foreground_bypass_batches_to_durable_completion(self) -> None:
        from app.service.verification.workflow import _publish_generate_task, _publish_run_task

        execution = self._execution()
        service = runtime.judgehost_task_service
        with reporting_judgehost(service, lambda work: JudgehostReply(output=b"1\n")):
            rows = runtime.verification_task_store.list_rows(execution.verification_id)
            generator = next(row for row in rows if row["task_kind"] == "generate-input")
            published = _publish_generate_task(generator, execution=execution, test_plan=execution.test_plan_by_name["001.in"])
            self.assertIsNone(published.terminal_result)
            self.assertEqual(service.wait_for_task_result(published.judgehost_task_id, timeout_sec=5)["task_status"], "completed")
            accepted = next(row for row in runtime.verification_task_store.list_rows(execution.verification_id) if row["task_kind"] == "main-correct")
            published = _publish_run_task(accepted, execution=execution)
            self.assertIsNone(published.terminal_result)
            self.assertEqual(service.wait_for_task_result(published.judgehost_task_id, timeout_sec=5)["task_status"], "completed")
            service.close_programs(execution.verification_id, ["generator-0", "accepted"])
            batches = self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
        self.assertTrue(all(batch["service_class"] == "foreground" and batch["bypass_case_result_cache"] == 1 for batch in batches))
        snapshot = runtime.verification_service.verification_snapshot(execution.verification_id)
        assert snapshot is not None
        self.assertEqual([row["status"] for row in snapshot["tasks"]], [VerificationTaskStatus.DONE, VerificationTaskStatus.DONE])
        self.assertEqual(snapshot["record"]["status"], "running")
        self.assertEqual(snapshot["detail"]["sanity_status"], "running")

    def test_sanity_stability_probe_outcomes_preserve_later_checks(self) -> None:
        scenarios = (
            ("wrong-answer", "wrong-answer", "passed", "", ["passed", "passed"], 2),
            ("correct", "correct", "failed", EMPTY_OUTPUT_STABILITY_CHECK, ["failed", "failed"], 0),
            ("wrong-answer", "internal-error", "failed", UNICODE_OUTPUT_STABILITY_CHECK, ["passed", "failed"], 1),
        )
        for first, second, status, failed_check, probe_statuses, count in scenarios:
            with self.subTest(first=first, second=second):
                verification_id, logs_dir = self._seed_sanity()

                def reply(work: DomjudgeWork) -> JudgehostReply:
                    return JudgehostReply(runresult=first if self._source_name(work) == "sanity_empty_output.py" else second)

                with reporting_judgehost(runtime.judgehost_task_service, reply):
                    result = runtime.verification_sanity_service.run(
                        problem=self.problem, user=self.user, verification_id=verification_id,
                        logs_dir=logs_dir, test_plans=[sanity_test_plan()], bypass_case_result_cache=True,
                    )
                    self._assert_closed_batches(verification_id, {"sanity_empty_output.py", "sanity_unicode_output.py"})
                self.assertEqual((result.status, result.check_name, result.checked_count), (status, failed_check, count), result.error)
                self.assertEqual([item.name for item in result.check_results[:2]], [EMPTY_OUTPUT_STABILITY_CHECK, UNICODE_OUTPUT_STABILITY_CHECK])
                self.assertEqual([item.status for item in result.check_results[:2]], probe_statuses)
                self.assertEqual((logs_dir / "boundary.log").read_text(), "boundary coverage ok\n")
                self.assertEqual((logs_dir / "summary-runtime-threshold.log").read_text(), "summary runtime threshold ok\n")
                self.assertIn("unicode_output_stability 001.in:", (logs_dir / "stability.log").read_text())

    def test_sanity_probe_errors_release_real_admitted_batches(self) -> None:
        service = runtime.judgehost_task_service
        for failure in ("enqueue-first", "enqueue-second", "wait", "close"):
            with self.subTest(failure=failure):
                verification_id, logs_dir = self._seed_sanity()
                put_bytes = runtime.runtime_blob_store.put_bytes
                wait_case = service.wait_for_task_case_result
                close_programs = service.close_programs

                def write_source(payload: bytes) -> PayloadFile:
                    if failure == "enqueue-first" and payload == b"import sys\n":
                        raise OSError("probe source write failed")
                    if failure == "enqueue-second" and payload.startswith(b"import base64\n"):
                        raise OSError("probe source write failed")
                    return put_bytes(payload)

                def receive(task_id: str, test_name: str, timeout_sec: float | None = None) -> CaseTerminalReport:
                    report = wait_case(task_id, test_name, timeout_sec)
                    if failure == "wait" and "empty" in report["summary"].get("source", ""):
                        raise OSError("probe result delivery failed")
                    return report

                def close(scope: str, programs: list[str]) -> None:
                    close_programs(scope, programs)
                    if failure == "close" and "unicode" in programs[0]:
                        raise OSError("probe close acknowledgement failed")

                with reporting_judgehost(service, lambda work: JudgehostReply(runresult="wrong-answer")), patch.object(
                    runtime.runtime_blob_store, "put_bytes", side_effect=write_source,
                ), patch.object(service, "wait_for_task_case_result", side_effect=receive), patch.object(
                    service, "close_programs", side_effect=close,
                ):
                    result = runtime.verification_sanity_service.run(
                        problem=self.problem, user=self.user, verification_id=verification_id,
                        logs_dir=logs_dir, test_plans=[sanity_test_plan()], bypass_case_result_cache=True,
                    )
                    expected_sources = {"sanity_empty_output.py", "sanity_unicode_output.py"}
                    if failure == "enqueue-first":
                        expected_sources.remove("sanity_empty_output.py")
                    elif failure == "enqueue-second":
                        expected_sources.remove("sanity_unicode_output.py")
                    self._assert_closed_batches(verification_id, expected_sources)
                self.assertEqual(result.status, "failed", result.error)
                self.assertCountEqual([item.status for item in result.check_results[:2]], ["failed", "passed"])
                self.assertIn("failed", (logs_dir / "stability.log").read_text())
                self.assertEqual((logs_dir / "boundary.log").read_text(), "boundary coverage ok\n")

    def test_sanity_independent_check_exception_closes_both_real_programs(self) -> None:
        verification_id, logs_dir = self._seed_sanity()
        with reporting_judgehost(runtime.judgehost_task_service, lambda work: JudgehostReply(runresult="wrong-answer")):
            with self.assertRaisesRegex(RuntimeError, "column summary"):
                runtime.verification_sanity_service.run(
                    problem=self.problem, user=self.user, verification_id=verification_id,
                    logs_dir=logs_dir, test_plans=[sanity_test_plan()], runtime_columns=[{"summary": None}],
                    bypass_case_result_cache=True,
                )
            self._assert_closed_batches(verification_id, {"sanity_empty_output.py", "sanity_unicode_output.py"})

    def test_sample_validation_completes_while_stability_probes_are_still_running(self) -> None:
        verification_id, logs_dir = self._seed_sanity()
        sample_received = threading.Event()

        def reply(work: DomjudgeWork) -> JudgehostReply:
            if self._source_name(work) == "custom_sample_output.py":
                sample_received.set()
                return JudgehostReply(output=b"1\n")
            deadline = time.monotonic() + 5
            while not (logs_dir / "validate.log").is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError("sample validation blocked behind a running stability probe")
                time.sleep(0.01)
            self.assertEqual((logs_dir / "validate.log").read_text(), "001.in: ok\n")
            return JudgehostReply(runresult="wrong-answer")

        with ExitStack() as peers:
            for _ in range(3):
                peers.enter_context(reporting_judgehost(runtime.judgehost_task_service, reply))
            result = runtime.verification_sanity_service.run(
                problem=self.problem, user=self.user, verification_id=verification_id,
                logs_dir=logs_dir, test_plans=[sanity_test_plan(sample=True, sample_output_text="1\n")],
                bypass_case_result_cache=True,
            )
            self._assert_closed_batches(verification_id, {"sanity_empty_output.py", "sanity_unicode_output.py", "custom_sample_output.py"})
        self.assertTrue(sample_received.is_set())
        self.assertEqual((result.status, result.checked_count), ("passed", 3), result.error)

    def test_sanity_missing_validator_boundary_warning_keeps_verification_ok(self) -> None:
        from app.service.verification.boundary_coverage import MISSING_VALIDATOR_MESSAGE

        verification_id, logs_dir = self._seed_sanity()
        with reporting_judgehost(runtime.judgehost_task_service, lambda work: JudgehostReply(runresult="wrong-answer")):
            result = runtime.verification_sanity_service.run(
                problem=self.problem, user=self.user, verification_id=verification_id,
                logs_dir=logs_dir, test_plans=[sanity_test_plan()], validator_configured=False,
                bypass_case_result_cache=True,
            )
            self._assert_closed_batches(verification_id, {"sanity_empty_output.py", "sanity_unicode_output.py"})
        self.assertEqual((result.status, result.check_name, result.checked_count), ("warning", BOUNDARY_COVERAGE_CHECK, 2))
        self.assertEqual(result.error, MISSING_VALIDATOR_MESSAGE)
        self.assertIn(MISSING_VALIDATOR_MESSAGE, (logs_dir / "boundary.log").read_text())

    def test_sanity_runtime_threshold_warning_uses_answer_correct_summary(self) -> None:
        verification_id, logs_dir = self._seed_sanity()
        columns = [
            {"source": source, "summary_has_tl": verdict == "TL", "summary": {"tests": [
                {"test": name, "verdict": verdict, "time_user_ms": elapsed, "answer_correct": True}
                for name, elapsed in (("001.in", 600), ("002.in", 1200))
            ]}}
            for source, verdict in (("solutions/accepted.cpp", "OK"), ("solutions/tle.cpp", "TL"))
        ]
        with reporting_judgehost(runtime.judgehost_task_service, lambda work: JudgehostReply(runresult="wrong-answer")):
            result = runtime.verification_sanity_service.run(
                problem=self.problem, user=self.user, verification_id=verification_id, logs_dir=logs_dir,
                test_plans=[sanity_test_plan(test_name="001.in"), sanity_test_plan(test_name="002.in")],
                runtime_columns=columns, time_limit_ms=1000, bypass_case_result_cache=True,
            )
            self._assert_closed_batches(verification_id, {"sanity_empty_output.py", "sanity_unicode_output.py"})
        self.assertEqual((result.status, result.check_name), ("warning", SUMMARY_RUNTIME_THRESHOLD_CHECK))
        self.assertEqual(result.error, "solutions/accepted.cpp: accepted solution is close to the time limit.")
        threshold = next(item for item in result.check_results if item.name == SUMMARY_RUNTIME_THRESHOLD_CHECK)
        self.assertEqual([message.message for message in threshold.messages], [
            "solutions/accepted.cpp: accepted solution is close to the time limit.",
            "solutions/tle.cpp: correct output in 50% extra time limit.",
        ])
        self.assertEqual(next(item.status for item in result.check_results if item.name == BOUNDARY_COVERAGE_CHECK), "passed")

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
            ("Main.java", b"public class Main { public static void main(String[] a) {} }\n"),
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
                    problem_mode="pass-fail",
                    pass_limit=1,
                    snapshot_root=accepted_file.path.parent,
                    artifact_file_by_test_ref={},
                    program_by_id={program.program_id: program},
                    execution_template_by_program_id={},
                    test_plan_by_name={},
                    run_verification_payload_base=self._payload_base(),
                    generate_verification_payload_base=self._payload_base(),
                    bypass_case_result_cache=False,
                    service_class="background",
                    judgehost=runtime.judgehost_task_service,
                    runtime_blob_store=runtime.runtime_blob_store,
                    verification_service=runtime.verification_service,
                    task_store=runtime.verification_task_store,
                )
                template = _execution_template(execution, program=program)
                self.assertEqual(template.upload_filename, source_name)
                self.assertEqual(template.submission.source_name, source_name)
                self.assertEqual(template.submission.source_file.path.read_bytes(), source_content)

    def test_execution_transition_commits_before_runtime_drain_and_rollback_keeps_work_runnable(self) -> None:
        for outcome in ("cancelled", "failed", "rollback"):
            with self.subTest(outcome=outcome):
                execution = self._execution()
                verification_id = execution.verification_id
                published = self._publish_generator(execution)
                entered, release = threading.Event(), threading.Event()
                write_transaction = runtime.db.write_transaction

                def blocked_write(transaction):
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("terminal transition storage was not released")
                    return write_transaction(transaction)

                if outcome == "rollback":
                    isolated_db_execute(runtime.db, f"""
                        CREATE TRIGGER adapter_cancel_abort BEFORE UPDATE OF status ON verifications
                        WHEN NEW.id='{verification_id}' AND NEW.status='cancelled'
                        BEGIN SELECT RAISE(ABORT, 'forced cancellation failure'); END
                    """)
                transition = (runtime.verification_execution_service.fail_verification if outcome == "failed"
                              else runtime.verification_execution_service.cancel_verification)
                try:
                    with patch.object(runtime.db, "write_transaction", side_effect=blocked_write):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pending = pool.submit(transition, verification_id, reason="terminal fixture")
                            try:
                                self.assertTrue(entered.wait(timeout=2))
                                self.assertEqual(runtime.verification_service.verification_record(verification_id)["status"], "running")
                                self.assertEqual([row["status"] for row in runtime.judgehost_task_service.run_case_snapshots(published.run_id)], ["cache-pending"])
                                self.assertFalse(pending.done())
                            finally:
                                release.set()
                            if outcome == "rollback":
                                with self.assertRaisesRegex(sqlite3.IntegrityError, "forced cancellation failure"):
                                    pending.result(timeout=5)
                            else:
                                self.assertEqual(pending.result(timeout=5).transition.outcome, "transitioned")
                finally:
                    if outcome == "rollback":
                        isolated_db_execute(runtime.db, "DROP TRIGGER IF EXISTS adapter_cancel_abort")
                if outcome == "rollback":
                    # Actual external execution still completes the admitted work after a failed cancellation.
                    with reporting_judgehost(runtime.judgehost_task_service, lambda work: JudgehostReply(output=b"1\n")):
                        self._run_execution(execution)
                        self._assert_closed_batches(verification_id, {"manual_validate.cpp", "std.cpp"})
                    snapshot = runtime.verification_service.verification_snapshot(verification_id)
                    assert snapshot is not None
                    self.assertTrue(all(row["status"] == VerificationTaskStatus.DONE for row in snapshot["tasks"]))
                else:
                    self._assert_durable_terminal(verification_id, outcome)
                    self._assert_closed_batches(verification_id, {"manual_validate.cpp"})
                    hostname = self.random_id("after-cancel")
                    runtime.judgehost_task_service.domjudge_register_host(hostname)
                    self.assertEqual(runtime.judgehost_task_service.domjudge_fetch_work(hostname), [])

    def test_cancellation_before_or_during_registration_prevents_new_runtime_work(self) -> None:
        registry = runtime.verification_runtime_registry
        register = registry.register
        for during_registration in (False, True):
            with self.subTest(during_registration=during_registration):
                execution = self._execution()

                def cancel_after_register(verification_id, handle, *, defers_finalization=False):
                    register(verification_id, handle, defers_finalization=defers_finalization)
                    runtime.verification_execution_service.cancel_verification(verification_id, reason="registration race")

                if during_registration:
                    with patch.object(registry, "register", side_effect=cancel_after_register):
                        self._run_execution(execution)
                else:
                    runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="before registration")
                    self._run_execution(execution)
                self._assert_durable_terminal(execution.verification_id, "cancelled")
                admitted = [runtime.judgehost_task_service.task_snapshot_for_run(run_id)
                            for run_id in runtime.judgehost_task_service.problem_run_ids(self.problem)]
                self.assertEqual([row for row in admitted if row and row["verification_id"] == execution.verification_id], [])

    def test_failed_cancel_event_stops_real_coordinator_and_drains_its_admitted_job(self) -> None:
        execution = self._execution()
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self._run_execution, execution)
            try:
                deadline = time.monotonic() + 5
                while not any(row["status"] == VerificationTaskStatus.QUEUED
                              for row in runtime.verification_task_store.list_rows(execution.verification_id)):
                    if time.monotonic() >= deadline:
                        self.fail("real coordinator did not admit generator")
                    time.sleep(0.01)
                with patch.object(runtime.verification_runtime_registry, "cancelled", side_effect=OSError("cancel event unavailable")):
                    runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="event failure")
                running.result(timeout=5)
            finally:
                runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="test cleanup")
        self._assert_durable_terminal(execution.verification_id, "cancelled")
        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp"})

    def test_closed_cancellation_can_retry_failed_runtime_drain(self) -> None:
        execution = self._execution()
        published = self._publish_generator(execution)
        with patch.object(runtime.judgehost_task_service, "request_verification_cancel", side_effect=OSError("drain unavailable")):
            with self.assertRaisesRegex(OSError, "drain unavailable"):
                runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="retry drain")
        self._assert_durable_terminal(execution.verification_id, "cancelled")
        self.assertEqual([row["status"] for row in runtime.judgehost_task_service.run_case_snapshots(published.run_id)], ["cache-pending"])
        retry = runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="retry drain")
        self.assertEqual(retry.transition.outcome, "closed")
        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp"})

    def test_scheduler_and_source_failures_terminalize_durable_graph_and_runtime(self) -> None:
        for failure in ("scheduler", "source"):
            with self.subTest(failure=failure):
                execution = self._execution()
                if failure == "scheduler":
                    probe = runtime.judgehost_task_service.probe_task_case_cache

                    def fail_after_real_probe(task_ids):
                        probe(task_ids)
                        raise OSError("runtime probe failed")

                    with patch.object(runtime.judgehost_task_service, "probe_task_case_cache", side_effect=fail_after_real_probe):
                        with self.assertRaisesRegex(VerificationCoordinatorFailure, "runtime probe failed"):
                            self._run_execution(execution)
                    self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp"})
                else:
                    execution.program_by_id["generator-0"].compile_spec.source_file.path.unlink()
                    self._run_execution(execution)
                self._assert_durable_terminal(execution.verification_id, "failed")
