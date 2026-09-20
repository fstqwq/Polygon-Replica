import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.service.judgehost.domjudge.wire_model import DomjudgeWork
from app.service.verification.execution import VerificationCoordinatorFailure
from app.service.verification.runtime_registry import VerificationRuntimeAlreadyRegistered
from app.service.verification.task_scheduler import VerificationRuntimeCoordinator, VerificationRuntimeCallbacks
from app.service.verification.types import VerificationTaskStatus
from app.service.verification.workflow import TaskExecutionContext

from tests.common import runtime
from tests.judgehost_support import JudgehostReply, reporting_judgehost
from tests.verification_adapter_fixture import VerificationExecutionTestBase


class TestVerificationRuntimeCoordinator(VerificationExecutionTestBase):
    def _wait_until(self, predicate: Callable[[], bool], message: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    @contextmanager
    def _running(
        self, execution: TaskExecutionContext, *, coordinator: VerificationRuntimeCoordinator | None = None,
    ) -> Iterator[Future[None]]:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self._run_execution, execution) if coordinator is None else pool.submit(coordinator.run)
            try:
                yield running
            finally:
                if not running.done():
                    runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="test shutdown")
                running.result(timeout=10)

    @staticmethod
    def _echo_reply(work: DomjudgeWork) -> JudgehostReply:
        service = runtime.judgehost_task_service
        case = service.case_snapshot(work["judgetaskid"])
        assert case is not None
        context = runtime.verification_task_store.bound_task_context(case["verification_task_id"])
        assert context is not None
        rows = {row["id"]: row for row in runtime.verification_task_store.list_rows(context["verification_id"])}
        row = rows[case["verification_task_id"]]
        predecessor = row["predecessor_task_id"]
        if predecessor:
            assert rows[predecessor]["status"] == VerificationTaskStatus.DONE
            assert rows[predecessor]["result"].verdict == "OK"
        return JudgehostReply(output=runtime.runtime_blob_store.read(case["input_ref"]))

    @staticmethod
    def _report(hostname: str, work: DomjudgeWork, *, runresult: str = "correct") -> None:
        service = runtime.judgehost_task_service
        service.domjudge_update_judging(hostname, work["judgetaskid"], {"compile_success": "1"})
        service.domjudge_add_judging_run(hostname, work["judgetaskid"], {
            "runresult": runresult, "runtime": "0.001", "output_run": b"1\n",
            "metadata": b"time-used:cpu-time\ncpu-time:0.001\nwall-time:0.001\nmemory-bytes:1024\n",
            "compare_metadata": b"exitcode:42\n" if runresult == "correct" else b"exitcode:43\n",
        })

    @staticmethod
    def _coordinator(execution: TaskExecutionContext) -> VerificationRuntimeCoordinator:
        service = runtime.judgehost_task_service
        callbacks = VerificationExecutionTestBase._execution_callbacks(execution)
        rows = runtime.verification_task_store.list_rows(execution.verification_id)
        return VerificationRuntimeCoordinator(
            execution.verification_id, task_store=runtime.verification_task_store,
            completion_service=runtime.verification_task_completion_service,
            callbacks=VerificationRuntimeCallbacks(
                publish_task=callbacks.publish_task, probe_task_case_cache=callbacks.probe_task_case_cache,
                close_programs=callbacks.close_programs, finish_tasks=callbacks.finish_tasks,
                reconcile_expired_leases=callbacks.reconcile_expired_leases,
                cancel_execution=lambda reason: service.request_verification_cancel(execution.verification_id, reason),
            ),
            edges=[(row["predecessor_task_id"], row["id"]) for row in rows if row["predecessor_task_id"]],
        )

    def test_cached_results_cross_publication_batches_without_an_external_host(self) -> None:
        test_names = tuple(f"{index:03}.in" for index in range(1, 258))
        cold = self._execution(test_names=test_names, unique_inputs=True, bypass_case_result_cache=False, sanity_status="skipped")
        with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply):
            with self._running(cold) as running:
                running.result(timeout=60)
            self._assert_closed_batches(cold.verification_id, {"manual_validate.cpp", "std.cpp"})
        original = {(row["program_id"], row["test_name"]): row["result"]
                    for row in runtime.verification_task_store.list_rows(cold.verification_id)}
        warm = self._execution(test_names=test_names, unique_inputs=True, bypass_case_result_cache=False, sanity_status="skipped")
        with self._running(warm) as running:
            running.result(timeout=60)
        self._assert_closed_batches(warm.verification_id, {"manual_validate.cpp", "std.cpp"})
        snapshot = runtime.verification_service.verification_snapshot(warm.verification_id)
        assert snapshot is not None
        self.assertEqual(snapshot["record"]["status"], "ok")
        self.assertEqual({(row["program_id"], row["test_name"]): row["result"] for row in snapshot["tasks"]}, original)
        self.assertEqual(len(snapshot["tasks"]), 514)
        self.assertTrue(all(row["status"] == VerificationTaskStatus.DONE for row in snapshot["tasks"]))

    def test_failed_completion_events_reconcile_real_results_and_successors(self) -> None:
        for both_events_fail in (False, True):
            with self.subTest(both_events_fail=both_events_fail):
                execution = self._execution(sanity_status="skipped")
                hostname = self.random_id("event-host")
                runtime.judgehost_task_service.domjudge_register_host(hostname)
                with patch("app.service.verification.task_scheduler._IDLE_RECONCILIATION_SEC", 0.02), self._running(execution) as running:
                    work = runtime.judgehost_task_service.domjudge_fetch_work(hostname, max_batchsize=1)[0]
                    with ExitStack() as faults:
                        faults.enter_context(patch.object(VerificationRuntimeCoordinator, "enqueue_completion_committed", side_effect=OSError("completion event unavailable")))
                        if both_events_fail:
                            faults.enter_context(patch.object(VerificationRuntimeCoordinator, "enqueue_completion_reconciliation", side_effect=OSError("reconciliation event unavailable")))
                            with self.assertRaisesRegex(RuntimeError, "completion event delivery and durable reconciliation failed"):
                                self._report(hostname, work, runresult="wrong-answer")
                        else:
                            self._report(hostname, work)
                    if both_events_fail:
                        running.result(timeout=10)
                        self._assert_durable_terminal(execution.verification_id, "failed")
                        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp"})
                    else:
                        with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply):
                            running.result(timeout=10)
                        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
                        self.assertEqual(runtime.verification_service.verification_record(execution.verification_id)["status"], "ok")

    def test_runtime_ownership_rejects_duplicate_execution_without_disturbing_the_owner(self) -> None:
        execution = self._execution(sanity_status="skipped")
        registry = runtime.verification_runtime_registry
        first, second = self._coordinator(execution), self._coordinator(execution)
        registry.register(execution.verification_id, first, defers_finalization=True)
        try:
            with self.assertRaises(VerificationRuntimeAlreadyRegistered):
                registry.register(execution.verification_id, second)
            self.assertFalse(registry.unregister(execution.verification_id, second))
            with self.assertRaisesRegex(VerificationCoordinatorFailure, "already registered"):
                self._run_execution(execution)
            with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply), self._running(execution, coordinator=first) as running:
                running.result(timeout=10)
            self.assertEqual(runtime.verification_service.verification_record(execution.verification_id)["status"], "ok")
            self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
        finally:
            registry.unregister(execution.verification_id, first)

    def test_blocked_notification_allows_runtime_owner_replacement(self) -> None:
        execution = self._execution(sanity_status="skipped")
        published = self._publish_generator(execution)
        first, replacement = self._coordinator(execution), self._coordinator(execution)
        registry = runtime.verification_runtime_registry
        registry.register(execution.verification_id, first, defers_finalization=True)
        entered, release = threading.Event(), threading.Event()
        deliver = first.enqueue_case_leased

        def blocked_delivery(task_id: str) -> None:
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("lease notification was not released")
            deliver(task_id)

        hostname = self.random_id("blocked-host")
        runtime.judgehost_task_service.domjudge_register_host(hostname)
        try:
            with patch.object(first, "enqueue_case_leased", side_effect=blocked_delivery), ThreadPoolExecutor(max_workers=2) as pool:
                lease = pool.submit(runtime.judgehost_task_service.domjudge_fetch_work, hostname, 1)
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    changing = pool.submit(registry.unregister, execution.verification_id, first)
                    self.assertTrue(changing.result(timeout=2), "notification held the registry lock")
                    registry.register(execution.verification_id, replacement, defers_finalization=True)
                    self.assertFalse(registry.unregister(execution.verification_id, first))
                finally:
                    release.set()
                work = lease.result(timeout=5)[0]
                with self._running(execution, coordinator=replacement) as replacement_run:
                    self._report(hostname, work)
                    with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply):
                        replacement_run.result(timeout=10)
            self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
            self.assertEqual(runtime.judgehost_task_service.task_snapshot_for_run(published.run_id)["status"], "completed")
            self.assertEqual(runtime.verification_service.verification_record(execution.verification_id)["status"], "ok")
        finally:
            release.set()
            registry.unregister(execution.verification_id, replacement)
            registry.unregister(execution.verification_id, first)

    def test_lost_host_requeues_the_same_case_for_a_new_host(self) -> None:
        execution = self._execution(sanity_status="skipped")
        published = self._publish_generator(execution)
        service = runtime.judgehost_task_service
        hostname = self.random_id("lost-host")
        service.domjudge_register_host(hostname)
        work = service.domjudge_fetch_work(hostname, max_batchsize=1)[0]
        self.assertEqual(service.case_snapshot(work["judgetaskid"])["status"], "leased")

        class StaleHostClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or timezone.utc) + timedelta(hours=1)

        with patch("app.service.verification.task_scheduler._IDLE_RECONCILIATION_SEC", 0.02), self._running(execution) as running:
            with patch("app.service.judgehost.maintenance.service.datetime", StaleHostClock):
                self._wait_until(lambda: service.case_snapshot(work["judgetaskid"])["status"] == "pending", "lost host lease did not return to pending")
            with reporting_judgehost(service, self._echo_reply):
                running.result(timeout=10)
        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
        cases = service.run_case_snapshots(published.run_id)
        self.assertEqual([row["id"] for row in cases], [work["judgetaskid"]])
        self.assertEqual(cases[0]["status"], "reported")
        self.assertNotEqual(cases[0]["last_callback_hostname"], hostname)

    def test_lease_notification_retry_keeps_real_execution_completable(self) -> None:
        deliver = VerificationRuntimeCoordinator.enqueue_case_leased
        for fail_all in (False, True):
            with self.subTest(fail_all=fail_all):
                execution = self._execution(sanity_status="skipped")
                failed = threading.Event()

                def fail_delivery(coordinator, task_id):
                    if fail_all or not failed.is_set():
                        failed.set()
                        raise OSError("lease notification unavailable")
                    return deliver(coordinator, task_id)

                with self._running(execution) as running:
                    with patch.object(VerificationRuntimeCoordinator, "enqueue_case_leased", fail_delivery):
                        if fail_all:
                            hostname = self.random_id("failed-lease")
                            runtime.judgehost_task_service.domjudge_register_host(hostname)
                            with self.assertRaisesRegex(RuntimeError, "case-lease event delivery and retry failed"):
                                runtime.judgehost_task_service.domjudge_fetch_work(hostname, max_batchsize=1)
                            rows = runtime.verification_task_store.list_rows(execution.verification_id)
                            queued = next(row for row in rows if row["task_kind"] == "generate-input")
                            self.assertEqual(queued["status"], VerificationTaskStatus.QUEUED)
                            cases = runtime.judgehost_task_service.run_case_snapshots(queued["run_id"])
                            self.assertEqual([row["status"] for row in cases], ["pending"])
                            self.assertIsNone(cases[0]["lease_owner"])
                        else:
                            with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply):
                                running.result(timeout=10)
                    if fail_all:
                        with reporting_judgehost(runtime.judgehost_task_service, self._echo_reply):
                            running.result(timeout=10)
                self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp", "std.cpp"})
                self.assertEqual(runtime.verification_service.verification_record(execution.verification_id)["status"], "ok")

    def test_cancel_between_admission_and_publication_return_retires_waiting_cases(self) -> None:
        execution = self._execution(test_names=("001.in", "002.in", "003.in"), unique_inputs=True)
        entered, release = threading.Event(), threading.Event()
        bind = runtime.verification_task_store.bind_and_expose_judgehost_runtime

        def pause_after_admission(*args, **kwargs):
            result = bind(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("first admission was not released")
            return result

        with patch.object(runtime.verification_task_store, "bind_and_expose_judgehost_runtime", side_effect=pause_after_admission), self._running(execution) as running:
            try:
                self.assertTrue(entered.wait(timeout=2))
                runtime.verification_execution_service.cancel_verification(execution.verification_id, reason="cancel first admission")
            finally:
                release.set()
            running.result(timeout=10)
        self._assert_durable_terminal(execution.verification_id, "cancelled")
        self._assert_closed_batches(execution.verification_id, {"manual_validate.cpp"})
        rows = runtime.verification_task_store.list_rows(execution.verification_id)
        self.assertEqual(len([row for row in rows if row["run_id"]]), 1)

    def test_source_failure_prevents_independent_generators_from_being_admitted(self) -> None:
        execution = self._execution(test_names=("001.in", "002.in", "003.in"), unique_inputs=True)
        execution.program_by_id["generator-0"].compile_spec.source_file.path.unlink()
        self._run_execution(execution)
        self._assert_durable_terminal(execution.verification_id, "failed")
        rows = runtime.verification_task_store.list_rows(execution.verification_id)
        self.assertEqual(len([row for row in rows if row["status"] == VerificationTaskStatus.FAILED]), 1)
        self.assertEqual(len([row for row in rows if row["status"] == VerificationTaskStatus.CANCELLED]), 5)
        self.assertEqual(runtime.judgehost_task_service.problem_run_ids(self.problem), [])
