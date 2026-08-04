from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.api import Judgehost
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import (
    CompileSubmission,
    ExecutionBatchRow,
    ExecutionBatchSpec,
    JudgehostCaseRow,
)
from app.service.judgehost.identity import domjudge_submit_id
from app.service.platform.rwlock import WriterPriorityRWLock

from .db_fixture import DBTestBase


_NOW = "2026-07-29T00:00:00+00:00"
_HASH = "1" * 64
_COMPILE_KEY = "5" * 64


def _compile_submission() -> CompileSubmission:
    return CompileSubmission(
        compile_key=_COMPILE_KEY,
        submit_id=domjudge_submit_id(_COMPILE_KEY),
        source_name="solution.cpp",
        source_bytes=b"int main() { return 0; }\n",
        extra_source_items=(),
        compile_files=(),
    )


def _result(test_name: str, *, runresult: str = "correct", verdict: str = "OK"):
    return build_case_result(
        test_name=test_name,
        runresult=runresult,
        verdict=verdict,
        runtime_sec=0.001,
        cpu_sec=0.001,
        wall_sec=0.002,
        memory_kb=1024,
        score_text="",
        output_run_rel="",
        output_error_rel="",
        output_system_rel="",
        output_diff_rel="",
        metadata_rel="",
        compare_metadata_rel="",
        team_message_rel="",
        feedback_text="",
        feedback_files=[],
        answer_correct=False,
    )


def _finish_pending_case(store: BatchScheduler, batch_id: int, test_name: str) -> None:
    case = next(row for row in store.cases_for_batch(batch_id) if row["test_name"] == test_name)
    store.request_batch_case_results(
        batch_id,
        results={int(case["id"]): _result(test_name)},
        updated_at=_NOW,
    )


def _case_row(
    task_id: str,
    run_id: str,
    test_name: str,
    ordinal: int,
    *,
    status: str = "pending",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "test_name": test_name,
        "ordinal": ordinal,
        "scope_sequence": 1,
        "testcase_id": None,
        "testcase_hash": _HASH,
        "testcase_input_hash": _HASH,
        "testcase_answer_hash": _HASH,
        "input_ref": "",
        "answer_ref": "",
        "status": status,
    }


def _create_batch(
    store: BatchScheduler,
    *,
    task_id: str,
    run_id: str,
    work_root: str,
    case_rows: list[dict[str, object]],
    execution_signature: str | None = None,
) -> int:
    signature = hashlib.sha256(
        (execution_signature or f"signature-{task_id}").encode("utf-8")
    ).hexdigest()
    return store.create_batch_with_cases(
        task_id=task_id,
        run_id=run_id,
        execution_signature=signature,
        verification_id="ver-1",
        compile_key=_COMPILE_KEY,
        compile_submission=_compile_submission(),
        contest_id="default",
        mode="pass-fail",
        source_name="solution.cpp",
        source_path=f"{work_root}/source/solution.cpp",
        work_root=work_root,
        compile_hash="2" * 32,
        run_hash="3" * 32,
        compare_hash="4" * 32,
        source_hash=_HASH,
        compile_config_json="{}",
        run_config_json="{}",
        compare_config_json="{}",
        expected_behavior="accepted",
        verification_source="run.execute",
        bypass_case_result_cache=0,
        service_class="background",
        batch_spec=ExecutionBatchSpec(),
        created_at=_NOW,
        case_rows=case_rows,
    )


class TestJudgehostStateLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BatchScheduler(id_base=100)

    def test_execution_batch_lives_until_verification_finishes(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-append-first",
            run_id="run-append-first",
            work_root="/tmp/append-first",
            case_rows=[_case_row("task-append-first", "run-append-first", "001.in", 1)],
            execution_signature="shared-signature",
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        self.assertEqual(self.store.fetch_batch(batch_id)["status"], "open")
        self.assertIsNone(self.store.claim_batch_finalization(batch_id, now_text=_NOW))

        same_batch_id = _create_batch(
            self.store,
            task_id="task-later",
            run_id="run-later",
            work_root="/tmp/ignored-for-existing-batch",
            case_rows=[_case_row("task-later", "run-later", "002.in", 1)],
            execution_signature="shared-signature",
        )
        self.assertEqual(same_batch_id, batch_id)
        _finish_pending_case(self.store, batch_id, "002.in")
        self.assertEqual(self.store.fetch_batch(batch_id)["status"], "open")

        self.assertEqual(
            self.store.finish_verification_execution("ver-1", now_text=_NOW),
            [batch_id],
        )
        self.assertIsNotNone(self.store.claim_batch_finalization(batch_id, now_text=_NOW))
        self.assertIsNone(self.store.claim_batch_finalization(batch_id, now_text=_NOW))
        with self.assertRaisesRegex(RuntimeError, "execution is closed"):
            _create_batch(
                self.store,
                task_id="task-too-late",
                run_id="run-too-late",
                work_root="/tmp/too-late",
                case_rows=[_case_row("task-too-late", "run-too-late", "003.in", 1)],
                execution_signature="shared-signature",
            )
        self.assertEqual(len(self.store.cases_for_batch(batch_id)), 2)

    def test_task_cases_cannot_span_batches(self) -> None:
        first_batch = _create_batch(
            self.store,
            task_id="task-first-batch",
            run_id="run-first-batch",
            work_root="/tmp/first-batch",
            case_rows=[_case_row("task-first-batch", "run-first-batch", "001.in", 1)],
        )
        second_batch = _create_batch(
            self.store,
            task_id="task-second-batch",
            run_id="run-second-batch",
            work_root="/tmp/second-batch",
            case_rows=[_case_row("task-second-batch", "run-second-batch", "001.in", 1)],
        )
        self.assertNotEqual(first_batch, second_batch)
        with self.assertRaisesRegex(RuntimeError, "already belong"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                work_root="/tmp/second-batch",
                case_rows=[_case_row("task-first-batch", "run-first-batch", "002.in", 2)],
                execution_signature="signature-task-second-batch",
            )
        duplicate = _create_batch(
            self.store,
            task_id="task-first-batch",
            run_id="run-first-batch",
            work_root="/tmp/first-batch",
            case_rows=[_case_row("task-first-batch", "run-first-batch", "001.in", 1)],
            execution_signature="signature-task-first-batch",
        )
        self.assertEqual(duplicate, first_batch)
        reordered = _case_row("task-first-batch", "run-first-batch", "001.in", 2)
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                work_root="/tmp/first-batch",
                case_rows=[reordered],
                execution_signature="signature-task-first-batch",
            )
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                work_root="/tmp/first-batch",
                case_rows=[
                    _case_row("task-first-batch", "run-first-batch", "001.in", 1),
                    _case_row("task-first-batch", "run-first-batch", "002.in", 2),
                ],
                execution_signature="signature-task-first-batch",
            )

    def test_forget_runs_removes_set_indexes(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-first",
            run_id="run-first",
            work_root="/tmp/forget-linear",
            case_rows=[_case_row("task-first", "run-first", "001.in", 1)],
        )
        same_batch = _create_batch(
            self.store,
            task_id="task-second",
            run_id="run-second",
            work_root="/tmp/forget-linear",
            case_rows=[_case_row("task-second", "run-second", "002.in", 1)],
            execution_signature="signature-task-first",
        )
        self.assertEqual(same_batch, batch_id)

        self.assertIsInstance(self.store._case_ids_by_batch[batch_id], set)
        removed_batches = self.store.forget_runs(["run-first", "run-second"])

        self.assertEqual(removed_batches, 1)
        self.assertIsNone(self.store.fetch_batch(batch_id))

    def test_run_index_cancels_every_grouped_batch_without_history_scan(self) -> None:
        batch_ids = []
        for sequence in range(2):
            task_id = f"task-shared-run-{sequence}"
            batch_ids.append(
                _create_batch(
                    self.store,
                    task_id=task_id,
                    run_id=f"primary-run-{sequence}",
                    work_root=f"/tmp/shared-run-{sequence}",
                    case_rows=[_case_row(task_id, "shared-run", "001.in", 1)],
                )
            )

        same_batch = _create_batch(
            self.store,
            task_id="task-shared-run-late",
            run_id="shared-run",
            work_root="/tmp/shared-run-0",
            case_rows=[_case_row("task-shared-run-late", "shared-run", "002.in", 1)],
            execution_signature="signature-task-shared-run-0",
        )
        self.assertEqual(same_batch, batch_ids[0])
        self.assertEqual(self.store.batch_for_run("shared-run")["batch_id"], batch_ids[1])

        self.assertEqual(
            self.store.cancel_batches_for_runs(["shared-run"], now_text=_NOW),
            batch_ids,
        )
        self.assertEqual(
            [self.store.cases_for_batch(batch_id)[0]["status"] for batch_id in batch_ids],
            ["cancelled", "cancelled"],
        )

    def test_rows_keep_public_shapes_and_progress_uses_incremental_counts(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-shapes",
            run_id="run-shapes",
            work_root="/tmp/shapes",
            case_rows=[
                _case_row("task-shapes", "run-shapes", "001.in", 1),
                _case_row("task-shapes", "run-shapes", "002.in", 2),
            ],
        )
        batch = self.store.fetch_batch(batch_id)
        cases = self.store.lease_cases(batch_id, hostname="host-a", limit=1, now_text=_NOW)

        self.assertIsNotNone(batch)
        self.assertEqual(set(batch), set(ExecutionBatchRow.__annotations__))
        self.assertEqual(set(cases[0]), set(JudgehostCaseRow.__annotations__))
        self.assertEqual(
            self.store.case_progress_for_runs(["run-shapes"]),
            {"run-shapes": {"total": 2, "reported": 0, "leased": 1}},
        )


class TestWriterPriorityRWLock(unittest.TestCase):
    def test_waiting_writer_blocks_new_readers(self) -> None:
        lock = WriterPriorityRWLock()
        first_reader_entered = threading.Event()
        release_first_reader = threading.Event()
        writer_waiting = threading.Event()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        second_reader_entered = threading.Event()
        order: list[str] = []

        def first_reader() -> None:
            with lock.read_lock():
                first_reader_entered.set()
                release_first_reader.wait(timeout=2)

        def writer() -> None:
            writer_waiting.set()
            with lock.write_lock():
                order.append("writer")
                writer_entered.set()
                release_writer.wait(timeout=2)

        def second_reader() -> None:
            with lock.read_lock():
                order.append("reader")
                second_reader_entered.set()

        threads = [
            threading.Thread(target=first_reader),
            threading.Thread(target=writer),
            threading.Thread(target=second_reader),
        ]
        threads[0].start()
        self.assertTrue(first_reader_entered.wait(timeout=2))
        threads[1].start()
        self.assertTrue(writer_waiting.wait(timeout=2))
        time.sleep(0.01)
        threads[2].start()
        self.assertFalse(second_reader_entered.wait(timeout=0.05))
        release_first_reader.set()
        self.assertTrue(writer_entered.wait(timeout=2))
        self.assertFalse(second_reader_entered.wait(timeout=0.05))
        release_writer.set()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(order, ["writer", "reader"])

    def test_lock_rejects_recursive_acquisition(self) -> None:
        lock = WriterPriorityRWLock()
        with lock.read_lock():
            with self.assertRaisesRegex(RuntimeError, "non-reentrant"):
                with lock.write_lock():
                    pass


class TestJudgehostLifecycle(DBTestBase):
    def _service(self) -> Judgehost:
        service = Judgehost(
            self.db,
            self.workspace_service,
            self.fs_manager,
            self.settings,
            self.constants,
            verification_task_store=self.verification_task_store,
            judge_fs_index_service=self.judge_fs_index_service,
        )
        service.state.enabled = True
        service.state.api_token = "test-token"
        service.state.api_username = "judgehost"
        service.state.include_build_payload = True
        return service

    def _work_root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="judgehost-lifecycle-")).resolve()
        (root / "source").mkdir()
        (root / "source" / "solution.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, root, True)
        return root

    @staticmethod
    def _add_task(service: Judgehost, task_id: str, run_id: str) -> None:
        service.state.task_registry.insert(
            {
                "id": task_id,
                "run_id": run_id,
                "problem_slug": "owner/problem",
                "username": "owner",
                "artifact_verification_id": "",
                "verification_id": "",
                "mode": "pass-fail",
                "status": service.STATUS_LEASED,
                "payload": {
                    "task_kind": "solution-run",
                    "verification_source": "run.execute",
                    "source_path": "solutions/ac.cpp",
                },
                "result": {},
                "persist_verification_run": False,
                "error_text": "",
                "created_at": _NOW,
                "updated_at": _NOW,
                "completed_at": "",
                "summary": {
                    "source": "solutions/ac.cpp",
                    "tests": [],
                    "compile_diagnostics": [],
                },
                "enqueue_fingerprint": "",
            }
        )

    @staticmethod
    def _task(service: Judgehost, task_id: str) -> dict[str, object]:
        row = service.state.task_registry.get(task_id)
        assert row is not None
        return row

    @staticmethod
    def _report_case(store: BatchScheduler, case_id: int, hostname: str) -> bool:
        claim = store.claim_case_reporting(
            case_id,
            hostname=hostname,
            now_text=_NOW,
        )
        if claim is None:
            return False
        return store.commit_case_result(
            case_id,
            generation=claim.generation,
            result=_result(claim.test_name),
            updated_at=_NOW,
        ) == "reported"

    def test_task_waits_for_all_own_cases_before_batch_cleanup(self) -> None:
        service = self._service()
        store = service.state.batch_scheduler
        task_id, run_id = "task-two-cases", "run-two-cases"
        self._add_task(service, task_id, run_id)
        work_root = self._work_root()
        batch_id = _create_batch(
            store,
            task_id=task_id,
            run_id=run_id,
            work_root=str(work_root),
            case_rows=[
                _case_row(task_id, run_id, "001.in", 1),
                _case_row(task_id, run_id, "002.in", 2),
            ],
        )
        store.record_compile_result(
            batch_id,
            compile_success=1,
            compile_output_b64="",
            compile_metadata_b64="",
            lease_owner="host-a",
            updated_at=_NOW,
        )
        cases = store.lease_cases(batch_id, hostname="host-a", limit=2, now_text=_NOW)

        self.assertTrue(self._report_case(store, int(cases[0]["id"]), "host-a"))
        service.result._domjudge_finalize_batch_if_ready(batch_id)
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertEqual(store.fetch_batch(batch_id)["status"], "open")
        self.assertTrue(work_root.is_dir())

        self.assertTrue(self._report_case(store, int(cases[1]["id"]), "host-a"))
        service.result._domjudge_finalize_batch_if_ready(batch_id)
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_COMPLETED)
        self.assertEqual(store.fetch_batch(batch_id)["status"], "open")
        self.assertTrue(work_root.is_dir())

        service.schedule_verification_cleanup("ver-1")
        self.assertEqual(store.fetch_batch(batch_id)["status"], "completed")
        self.assertFalse(work_root.exists())

    def test_reporting_abort_finalizes_deferred_cancel(self) -> None:
        service = self._service()
        store = service.state.batch_scheduler
        task_id, run_id = "task-abort-cancel", "run-abort-cancel"
        self._add_task(service, task_id, run_id)
        work_root = self._work_root()
        batch_id = _create_batch(
            store,
            task_id=task_id,
            run_id=run_id,
            work_root=str(work_root),
            case_rows=[_case_row(task_id, run_id, "001.in", 1)],
        )
        case = store.lease_cases(batch_id, hostname="host-a", limit=1, now_text=_NOW)[0]

        def _cancel_then_fail(*_args, **_kwargs) -> int:
            self.assertEqual(store.cancel_batches_for_runs([run_id], now_text=_NOW), [batch_id])
            raise RuntimeError("artifact write failed")

        with patch.object(
            service.result,
            "_process_domjudge_judging_run",
            side_effect=_cancel_then_fail,
        ), self.assertRaisesRegex(RuntimeError, "artifact write failed"):
            service.domjudge_add_judging_run("host-a", int(case["id"]), {})

        service.schedule_verification_cleanup("ver-1")
        self.assertEqual(store.fetch_batch(batch_id)["status"], "failed")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_FAILED)
        self.assertFalse(work_root.exists())

    def test_grouped_batch_finalizes_each_task_once_across_hosts(self) -> None:
        service = self._service()
        store = service.state.batch_scheduler
        first_task, first_run = "task-group-a", "run-group-a"
        second_task, second_run = "task-group-b", "run-group-b"
        self._add_task(service, first_task, first_run)
        self._add_task(service, second_task, second_run)
        work_root = self._work_root()
        batch_id = _create_batch(
            store,
            task_id=first_task,
            run_id=first_run,
            work_root=str(work_root),
            execution_signature="shared-group",
            case_rows=[_case_row(first_task, first_run, "001.in", 1)],
        )
        appended_batch = _create_batch(
            store,
            task_id=second_task,
            run_id=second_run,
            work_root=str(work_root),
            case_rows=[_case_row(second_task, second_run, "002.in", 1)],
            execution_signature="shared-group",
        )
        self.assertEqual(appended_batch, batch_id)
        self.assertTrue(store.activate_task_cases(second_task, now_text=_NOW))
        second_case_id = int(store.cases_for_task(second_task)[0]["id"])
        cache_claims = store.claim_cache_cases(
            batch_id,
            hostname="cache-setup",
            limit=1,
            now_text=_NOW,
        )
        self.assertEqual([claim.case_id for claim, _row in cache_claims], [second_case_id])
        self.assertTrue(
            store.finish_cache_miss(
                second_case_id,
                generation=cache_claims[0][0].generation,
                updated_at=_NOW,
            )
        )
        self.assertEqual(store.batch_for_task(second_task)["batch_id"], batch_id)
        store.record_compile_result(
            batch_id,
            compile_success=1,
            compile_output_b64="",
            compile_metadata_b64="",
            lease_owner="host-a",
            updated_at=_NOW,
        )
        first_case = store.lease_cases(batch_id, hostname="host-a", limit=1, now_text=_NOW)[0]
        second_case = store.lease_cases(batch_id, hostname="host-b", limit=1, now_text=_NOW)[0]
        self.assertEqual(store.case_execution_row(int(first_case["id"]))["run_id"], first_run)
        self.assertEqual(store.case_execution_row(int(second_case["id"]))["run_id"], second_run)
        self.assertEqual(self._task(service, second_task)["status"], service.STATUS_LEASED)

        published: list[tuple[str, str]] = []
        original_publish = service.result._publish_verification_case_result
        original_finalize = service.queue.finalize_domjudge_task

        def record_publish(*, task_id: str, test_name: str, case_result: dict[str, object]) -> bool:
            published.append((task_id, test_name))
            return original_publish(task_id=task_id, test_name=test_name, case_result=case_result)

        with (
            patch.object(service.result, "_publish_verification_case_result", side_effect=record_publish),
            patch.object(service.queue, "finalize_domjudge_task", wraps=original_finalize) as finalize_task,
        ):
            self.assertFalse(self._report_case(store, int(first_case["id"]), "host-b"))
            self.assertTrue(self._report_case(store, int(first_case["id"]), "host-a"))
            service.result._domjudge_publish_reported_case(
                task_id=first_task,
                test_name="001.in",
            )
            service.result._domjudge_finalize_task_if_ready(
                first_task,
                batch_row=dict(store.batch_finalize_row(batch_id) or {}),
            )
            service.result._domjudge_finalize_batch_if_ready(batch_id)
            self.assertEqual(self._task(service, first_task)["status"], service.STATUS_COMPLETED)
            self.assertEqual(self._task(service, second_task)["status"], service.STATUS_LEASED)
            self.assertTrue(work_root.exists())
            self.assertIsNotNone(service.state.task_registry.get(first_task))

            self.assertTrue(self._report_case(store, int(second_case["id"]), "host-b"))
            service.result._domjudge_publish_reported_case(
                task_id=second_task,
                test_name="002.in",
            )
            service.result._domjudge_finalize_task_if_ready(
                second_task,
                batch_row=dict(store.batch_finalize_row(batch_id) or {}),
            )
            service.result._domjudge_finalize_batch_if_ready(batch_id)

        service.schedule_verification_cleanup("ver-1")

        self.assertEqual(finalize_task.call_count, 2)
        self.assertEqual(
            {first_task, second_task},
            {
                task_id
                for task_id in (first_task, second_task)
                if self._task(service, task_id)["status"] == service.STATUS_COMPLETED
            },
        )
        self.assertNotIn("internal-finalizer", service.state.hosts_state)
        self.assertTrue({(first_task, "001.in"), (second_task, "002.in")}.issubset(set(published)))
        self.assertEqual(store.fetch_batch(batch_id)["status"], "completed")
        self.assertFalse(work_root.exists())

    def test_finalizing_batch_retries_incomplete_steps_before_cleanup(self) -> None:
        service = self._service()
        store = service.state.batch_scheduler
        task_id, run_id = "task-finalize-retry", "run-finalize-retry"
        self._add_task(service, task_id, run_id)
        work_root = self._work_root()
        batch_id = _create_batch(
            store,
            task_id=task_id,
            run_id=run_id,
            work_root=str(work_root),
            case_rows=[_case_row(task_id, run_id, "001.in", 1)],
        )
        case_row = store.lease_cases(batch_id, hostname="host-a", limit=1, now_text=_NOW)[0]
        self.assertTrue(self._report_case(store, int(case_row["id"]), "host-a"))
        store.finish_verification_execution("ver-1", now_text=_NOW)

        with patch.object(
            service.result,
            "_domjudge_publish_reported_case",
            side_effect=RuntimeError("transient publish failure"),
        ), patch("app.service.judgehost.result.logger.exception"):
            service.result._domjudge_finalize_batch_if_ready(batch_id)

        self.assertEqual(store.fetch_batch(batch_id)["status"], "finalize-pending")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertTrue(work_root.exists())

        with patch.object(
            service.result,
            "_domjudge_finalize_task_if_ready",
            side_effect=RuntimeError("transient aggregation failure"),
        ), patch("app.service.judgehost.result.logger.exception"), patch(
            "app.service.judgehost.result.logger.error"
        ):
            service.result._domjudge_finalize_batch_if_ready(batch_id)

        self.assertEqual(store.fetch_batch(batch_id)["status"], "finalize-pending")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertTrue(work_root.exists())

        service.result._domjudge_finalize_batch_if_ready(batch_id)

        self.assertEqual(store.fetch_batch(batch_id)["status"], "completed")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_COMPLETED)
        self.assertFalse(work_root.exists())

    def test_cache_compile_failure_and_cancel_use_common_finalizer(self) -> None:
        for scenario in ("cache", "compile-failure", "cancel"):
            with self.subTest(scenario=scenario):
                service = self._service()
                store = service.state.batch_scheduler
                task_id, run_id = f"task-{scenario}", f"run-{scenario}"
                self._add_task(service, task_id, run_id)
                work_root = self._work_root()
                batch_id = _create_batch(
                    store,
                    task_id=task_id,
                    run_id=run_id,
                    work_root=str(work_root),
                    case_rows=[
                        _case_row(
                            task_id,
                            run_id,
                            "001.in",
                            1,
                            status="cache-pending" if scenario == "cache" else "pending",
                        ),
                        *(
                            [_case_row(task_id, run_id, "002.in", 2)]
                            if scenario == "cancel"
                            else []
                        ),
                    ],
                )
                if scenario == "cache":
                    case_id = int(store.cases_for_batch(batch_id)[0]["id"])
                    cache_claim = store.claim_cache_cases(
                        batch_id,
                        hostname="cache",
                        limit=1,
                        now_text=_NOW,
                    )[0][0]
                    self.assertEqual(
                        store.commit_case_result(
                            case_id,
                            generation=cache_claim.generation,
                            result=_result("001.in"),
                            updated_at=_NOW,
                        ),
                        "reported",
                    )
                elif scenario == "compile-failure":
                    store.record_compile_result(
                        batch_id,
                        compile_success=0,
                        compile_output_b64="",
                        compile_metadata_b64="",
                        lease_owner="host-a",
                        updated_at=_NOW,
                    )
                else:
                    leased = store.lease_cases(
                        batch_id,
                        hostname="host-a",
                        limit=1,
                        now_text=_NOW,
                    )
                    self.assertEqual(len(leased), 1)

                with patch.object(
                    service.result,
                    "_publish_verification_case_result",
                    wraps=service.result._publish_verification_case_result,
                ) as publish_case:
                    if scenario == "cancel":
                        self.assertEqual(
                            service.cancel_tasks_for_runs(
                                [run_id],
                                reason="verification cancelled by user",
                            ),
                            0,
                        )
                        self.assertEqual(
                            self._task(service, task_id)["status"],
                            service.STATUS_LEASED,
                        )
                        self.assertEqual(service.cancel_domjudge_batches_for_runs([run_id]), 1)
                    else:
                        service.result._domjudge_finalize_batch_if_ready(batch_id)
                service.schedule_verification_cleanup("ver-1")
                expected = service.STATUS_COMPLETED if scenario == "cache" else service.STATUS_FAILED
                self.assertEqual(self._task(service, task_id)["status"], expected)
                self.assertEqual(store.fetch_batch(batch_id)["status"], "completed" if scenario == "cache" else "failed")
                if scenario == "cancel":
                    self.assertEqual(publish_case.call_count, 2)
                self.assertTrue(
                    all(row["status"] in {"reported", "cancelled"} for row in store.cases_for_task(task_id))
                )
                self.assertFalse(work_root.exists())

    def test_run_id_is_idempotent_only_for_identical_payload(self) -> None:
        service = self._service()
        sequence = 0

        def build_payload(**kwargs) -> dict[str, object]:
            nonlocal sequence
            sequence += 1
            return {
                "source_name": "solution.cpp",
                "source_label": "solution.cpp",
                "source_b64": "eA==",
                "task_kind": "solution-run",
                "verification_payload": {},
                "selected_tests": list(kwargs["selected_tests"]),
                "enqueued_at": f"{_NOW}-{sequence}",
            }

        args = {
            "problem": self.problem,
            "username": self.user,
            "artifact_verification_id": "artifact",
            "mode": "pass-fail",
            "submission_path": "solutions/ac.cpp",
            "upload_content": None,
            "upload_filename": None,
            "run_id": "run-idempotent",
            "selected_tests": ["001.in"],
            "verification_id": "ver-1d3e0",
            "verification_run_ids": ["run-idempotent"],
            "expected_behavior": "accepted",
            "verification_source": "run.execute",
        }
        with (
            patch.object(service.enqueue, "_build_task_payload", side_effect=build_payload),
            patch.object(
                service.enqueue,
                "_domjudge_precomputed_fields_from_payload",
                return_value={"run_config": {"pass_limit": 1}},
            ),
            patch.object(service.dispatch, "stage_task", return_value=123),
            patch.object(service.state.batch_scheduler, "activate_task_cases", return_value=1),
        ):
            task_id = service.enqueue_task(**args)
            self.assertEqual(service.enqueue_task(**args), task_id)
            with self.assertRaisesRegex(RuntimeError, "run id reused with different payload"):
                service.enqueue_task(**{**args, "selected_tests": ["002.in"]})
