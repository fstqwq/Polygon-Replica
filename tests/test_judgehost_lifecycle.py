from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.api import Judgehost
from app.service.judgehost.job_scheduler import JobScheduler
from app.service.judgehost.job_scheduler_models import JobSpec, JudgehostCaseRow, JudgehostJobRow
from app.service.platform.rwlock import WriterPriorityRWLock

from .db_fixture import DBTestBase


_NOW = "2026-07-29T00:00:00+00:00"
_HASH = "1" * 64


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


def _create_job(
    store: JobScheduler,
    *,
    task_id: str,
    run_id: str,
    work_root: str,
    case_rows: list[dict[str, object]],
    group_key: str = "",
) -> int:
    return store.create_job_with_cases(
        task_id=task_id,
        run_id=run_id,
        group_key=group_key,
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
        force_recompile=0,
        service_class="background",
        job_spec=JobSpec(),
        created_at=_NOW,
        case_rows=case_rows,
    )


class TestJudgehostStateLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobScheduler(id_base=100)

    def test_append_and_finalization_claim_are_serializable(self) -> None:
        append_first_job = _create_job(
            self.store,
            task_id="task-append-first",
            run_id="run-append-first",
            work_root="/tmp/append-first",
            case_rows=[_case_row("task-append-first", "run-append-first", "001.in", 1, status="reported")],
        )
        later_case = _case_row("task-later", "run-later", "002.in", 1)
        later_case.pop("scope_sequence")
        appended = self.store.append_cases_to_job(
            job_id=append_first_job,
            case_rows=[later_case],
            now_text=_NOW,
        )
        self.assertEqual(appended["outcome"], "appended")
        self.assertEqual(self.store.cases_for_task("task-later")[0]["scope_sequence"], 1)
        self.assertIsNone(self.store.claim_job_finalization(append_first_job, now_text=_NOW))

        claim_first_job = _create_job(
            self.store,
            task_id="task-claim-first",
            run_id="run-claim-first",
            work_root="/tmp/claim-first",
            group_key="claim-group",
            case_rows=[_case_row("task-claim-first", "run-claim-first", "001.in", 1, status="reported")],
        )
        self.assertEqual(self.store.job_for_group_key("claim-group")["job_id"], claim_first_job)
        self.assertIsNotNone(self.store.claim_job_finalization(claim_first_job, now_text=_NOW))
        self.assertIsNone(self.store.job_for_group_key("claim-group"))
        closed = self.store.append_cases_to_job(
            job_id=claim_first_job,
            case_rows=[_case_row("task-too-late", "run-too-late", "002.in", 1)],
            now_text=_NOW,
        )
        self.assertEqual(closed["outcome"], "closed")
        self.assertEqual(len(self.store.cases_for_job(claim_first_job)), 1)

    def test_task_cases_cannot_span_jobs(self) -> None:
        first_job = _create_job(
            self.store,
            task_id="task-first-job",
            run_id="run-first-job",
            work_root="/tmp/first-job",
            case_rows=[_case_row("task-first-job", "run-first-job", "001.in", 1)],
        )
        second_job = _create_job(
            self.store,
            task_id="task-second-job",
            run_id="run-second-job",
            work_root="/tmp/second-job",
            case_rows=[_case_row("task-second-job", "run-second-job", "001.in", 1)],
        )
        self.assertNotEqual(first_job, second_job)
        with self.assertRaisesRegex(RuntimeError, "already belong"):
            self.store.append_cases_to_job(
                job_id=second_job,
                case_rows=[_case_row("task-first-job", "run-first-job", "002.in", 2)],
                now_text=_NOW,
            )
        duplicate = self.store.append_cases_to_job(
            job_id=first_job,
            case_rows=[_case_row("task-first-job", "run-first-job", "001.in", 1)],
            now_text=_NOW,
        )
        self.assertEqual(duplicate["outcome"], "duplicate")
        reordered = _case_row("task-first-job", "run-first-job", "001.in", 2)
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            self.store.append_cases_to_job(
                job_id=first_job,
                case_rows=[reordered],
                now_text=_NOW,
            )
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            self.store.append_cases_to_job(
                job_id=first_job,
                case_rows=[
                    _case_row("task-first-job", "run-first-job", "001.in", 1),
                    _case_row("task-first-job", "run-first-job", "002.in", 2),
                ],
                now_text=_NOW,
            )

    def test_forget_runs_filters_case_lists_without_repeated_remove(self) -> None:
        job_id = _create_job(
            self.store,
            task_id="task-first",
            run_id="run-first",
            work_root="/tmp/forget-linear",
            case_rows=[_case_row("task-first", "run-first", "001.in", 1)],
        )
        self.store.append_cases_to_job(
            job_id=job_id,
            case_rows=[_case_row("task-second", "run-second", "002.in", 1)],
            now_text=_NOW,
        )

        class _NoRemoveList(list[int]):
            def remove(self, value: int) -> None:
                raise AssertionError(f"list.remove called for {value}")

        self.store._case_ids_by_job[job_id] = _NoRemoveList(
            self.store._case_ids_by_job[job_id]
        )
        removed_jobs = self.store.forget_runs(["run-first", "run-second"])

        self.assertEqual(removed_jobs, 1)
        self.assertIsNone(self.store.fetch_job(job_id))

    def test_append_and_claim_race_has_one_serializable_winner(self) -> None:
        for sequence in range(20):
            store = JobScheduler(id_base=1000 + sequence * 10)
            job_id = _create_job(
                store,
                task_id=f"task-race-{sequence}",
                run_id=f"run-race-{sequence}",
                work_root=f"/tmp/race-{sequence}",
                case_rows=[
                    _case_row(
                        f"task-race-{sequence}",
                        f"run-race-{sequence}",
                        "001.in",
                        1,
                        status="reported",
                    )
                ],
            )
            barrier = threading.Barrier(3)
            outcomes: dict[str, object] = {}

            def append() -> None:
                barrier.wait()
                outcomes["append"] = store.append_cases_to_job(
                    job_id=job_id,
                    case_rows=[
                        _case_row(
                            f"task-later-{sequence}",
                            f"run-later-{sequence}",
                            "002.in",
                            1,
                        )
                    ],
                    now_text=_NOW,
                )

            def claim() -> None:
                barrier.wait()
                outcomes["claim"] = store.claim_job_finalization(job_id, now_text=_NOW)

            append_thread = threading.Thread(target=append)
            claim_thread = threading.Thread(target=claim)
            append_thread.start()
            claim_thread.start()
            barrier.wait()
            append_thread.join(timeout=2)
            claim_thread.join(timeout=2)

            append_result = outcomes["append"]
            claim_result = outcomes["claim"]
            self.assertIsInstance(append_result, dict)
            if claim_result is None:
                self.assertEqual(append_result["outcome"], "appended")
            else:
                self.assertEqual(append_result["outcome"], "closed")

    def test_run_index_cancels_every_grouped_job_without_history_scan(self) -> None:
        job_ids = []
        for sequence in range(2):
            task_id = f"task-shared-run-{sequence}"
            job_ids.append(
                _create_job(
                    self.store,
                    task_id=task_id,
                    run_id=f"primary-run-{sequence}",
                    work_root=f"/tmp/shared-run-{sequence}",
                    case_rows=[_case_row(task_id, "shared-run", "001.in", 1)],
                )
            )

        self.store.append_cases_to_job(
            job_id=job_ids[0],
            case_rows=[_case_row("task-shared-run-late", "shared-run", "002.in", 1)],
            now_text=_NOW,
        )
        self.assertEqual(self.store.job_for_run("shared-run")["job_id"], job_ids[1])

        self.assertEqual(
            self.store.cancel_jobs_for_runs(["shared-run"], now_text=_NOW),
            job_ids,
        )
        self.assertEqual(
            [self.store.cases_for_job(job_id)[0]["status"] for job_id in job_ids],
            ["cancelled", "cancelled"],
        )

    def test_rows_keep_public_shapes_and_progress_uses_incremental_counts(self) -> None:
        job_id = _create_job(
            self.store,
            task_id="task-shapes",
            run_id="run-shapes",
            work_root="/tmp/shapes",
            case_rows=[
                _case_row("task-shapes", "run-shapes", "001.in", 1),
                _case_row("task-shapes", "run-shapes", "002.in", 2),
            ],
        )
        job = self.store.fetch_job(job_id)
        cases = self.store.lease_cases(job_id, hostname="host-a", limit=1, now_text=_NOW)

        self.assertIsNotNone(job)
        self.assertEqual(set(job), set(JudgehostJobRow.__annotations__))
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
    def _report_case(store: JobScheduler, case_id: int, hostname: str) -> bool:
        return store.report_case_result(
            case_id,
            lease_owner=hostname,
            runresult="correct",
            runtime_sec=0.001,
            cpu_sec=0.001,
            wall_sec=0.002,
            memory_kb=1024,
            output_run_rel="",
            output_error_rel="",
            output_system_rel="",
            output_diff_rel="",
            metadata_rel="",
            compare_metadata_rel="",
            team_message_rel="",
            score_text="",
            updated_at=_NOW,
        )

    def test_task_waits_for_all_own_cases_before_job_cleanup(self) -> None:
        service = self._service()
        store = service.state.job_scheduler
        task_id, run_id = "task-two-cases", "run-two-cases"
        self._add_task(service, task_id, run_id)
        work_root = self._work_root()
        job_id = _create_job(
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
            job_id,
            compile_success=1,
            compile_output_b64="",
            compile_metadata_b64="",
            lease_owner="host-a",
            updated_at=_NOW,
        )
        cases = store.lease_cases(job_id, hostname="host-a", limit=2, now_text=_NOW)

        self.assertTrue(self._report_case(store, int(cases[0]["id"]), "host-a"))
        service.result._domjudge_finalize_if_ready(job_id)
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertEqual(store.fetch_job(job_id)["status"], "open")
        self.assertTrue(work_root.is_dir())

        self.assertTrue(self._report_case(store, int(cases[1]["id"]), "host-a"))
        service.result._domjudge_finalize_if_ready(job_id)
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_COMPLETED)
        self.assertEqual(store.fetch_job(job_id)["status"], "completed")
        self.assertFalse(work_root.exists())

    def test_grouped_job_finalizes_each_task_once_across_hosts(self) -> None:
        service = self._service()
        store = service.state.job_scheduler
        first_task, first_run = "task-group-a", "run-group-a"
        second_task, second_run = "task-group-b", "run-group-b"
        self._add_task(service, first_task, first_run)
        self._add_task(service, second_task, second_run)
        work_root = self._work_root()
        job_id = _create_job(
            store,
            task_id=first_task,
            run_id=first_run,
            work_root=str(work_root),
            group_key="shared-group",
            case_rows=[_case_row(first_task, first_run, "001.in", 1)],
        )
        appended = store.append_cases_to_job(
            job_id=job_id,
            case_rows=[_case_row(second_task, second_run, "002.in", 1)],
            now_text=_NOW,
        )
        self.assertEqual(appended["outcome"], "appended")
        self.assertTrue(store.activate_task_cases(second_task, now_text=_NOW))
        second_case_id = int(store.cases_for_task(second_task)[0]["id"])
        self.assertEqual(store.mark_cache_misses([second_case_id], now_text=_NOW), 1)
        self.assertEqual(store.job_for_task(second_task)["job_id"], job_id)
        store.record_compile_result(
            job_id,
            compile_success=1,
            compile_output_b64="",
            compile_metadata_b64="",
            lease_owner="host-a",
            updated_at=_NOW,
        )
        first_case = store.lease_cases(job_id, hostname="host-a", limit=1, now_text=_NOW)[0]
        second_case = store.lease_cases(job_id, hostname="host-b", limit=1, now_text=_NOW)[0]
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
                job_row=dict(store.job_finalize_row(job_id) or {}),
            )
            service.result._domjudge_finalize_if_ready(job_id)
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
                job_row=dict(store.job_finalize_row(job_id) or {}),
            )
            service.result._domjudge_finalize_if_ready(job_id)

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
        self.assertEqual(store.fetch_job(job_id)["status"], "completed")
        self.assertFalse(work_root.exists())

    def test_finalizing_job_retries_incomplete_steps_before_cleanup(self) -> None:
        service = self._service()
        store = service.state.job_scheduler
        task_id, run_id = "task-finalize-retry", "run-finalize-retry"
        self._add_task(service, task_id, run_id)
        work_root = self._work_root()
        job_id = _create_job(
            store,
            task_id=task_id,
            run_id=run_id,
            work_root=str(work_root),
            case_rows=[_case_row(task_id, run_id, "001.in", 1)],
        )
        case_row = store.lease_cases(job_id, hostname="host-a", limit=1, now_text=_NOW)[0]
        self.assertTrue(self._report_case(store, int(case_row["id"]), "host-a"))

        with patch.object(
            service.result,
            "_domjudge_publish_reported_case",
            side_effect=RuntimeError("transient publish failure"),
        ), patch("app.service.judgehost.result.logger.exception"):
            service.result._domjudge_finalize_if_ready(job_id)

        self.assertEqual(store.fetch_job(job_id)["status"], "finalizing")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertTrue(work_root.exists())

        with patch.object(
            service.result,
            "_domjudge_finalize_task_if_ready",
            side_effect=RuntimeError("transient aggregation failure"),
        ), patch("app.service.judgehost.result.logger.exception"), patch(
            "app.service.judgehost.result.logger.error"
        ):
            service.result._schedule_finalization_retry(job_id, delay_sec=0.0)
            service.result.retry_due_finalizations(limit=1)

        self.assertEqual(store.fetch_job(job_id)["status"], "finalizing")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_LEASED)
        self.assertTrue(work_root.exists())

        service.result._domjudge_finalize_if_ready(job_id)

        self.assertEqual(store.fetch_job(job_id)["status"], "completed")
        self.assertEqual(self._task(service, task_id)["status"], service.STATUS_COMPLETED)
        self.assertFalse(work_root.exists())

    def test_cache_compile_failure_and_cancel_use_common_finalizer(self) -> None:
        for scenario in ("cache", "compile-failure", "cancel"):
            with self.subTest(scenario=scenario):
                service = self._service()
                store = service.state.job_scheduler
                task_id, run_id = f"task-{scenario}", f"run-{scenario}"
                self._add_task(service, task_id, run_id)
                work_root = self._work_root()
                job_id = _create_job(
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
                    case_id = int(store.cases_for_job(job_id)[0]["id"])
                    store.apply_cached_case_results(
                        cached_rows=[
                            {
                                "case_id": case_id,
                                "runresult": "correct",
                                "runtime_sec": 0.001,
                                "cpu_sec": 0.001,
                                "wall_sec": 0.002,
                                "memory_kb": 1024,
                                "output_run_rel": "",
                                "output_error_rel": "",
                                "output_system_rel": "",
                                "output_diff_rel": "",
                                "metadata_rel": "",
                                "compare_metadata_rel": "",
                                "team_message_rel": "",
                                "score_text": "",
                            }
                        ],
                        lease_owner="cache",
                        now_text=_NOW,
                    )
                elif scenario == "compile-failure":
                    store.record_compile_result(
                        job_id,
                        compile_success=0,
                        compile_output_b64="",
                        compile_metadata_b64="",
                        lease_owner="host-a",
                        updated_at=_NOW,
                    )
                else:
                    leased = store.lease_cases(
                        job_id,
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
                        self.assertEqual(service.cancel_domjudge_jobs_for_runs([run_id]), 1)
                    else:
                        service.result._domjudge_finalize_if_ready(job_id)
                expected = service.STATUS_COMPLETED if scenario == "cache" else service.STATUS_FAILED
                self.assertEqual(self._task(service, task_id)["status"], expected)
                self.assertEqual(store.fetch_job(job_id)["status"], "completed" if scenario == "cache" else "failed")
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
            "verification_id": "verification-idempotent",
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
            patch.object(service.state.job_scheduler, "activate_task_cases", return_value=1),
        ):
            task_id = service.enqueue_task(**args)
            self.assertEqual(service.enqueue_task(**args), task_id)
            with self.assertRaisesRegex(RuntimeError, "run id reused with different payload"):
                service.enqueue_task(**{**args, "selected_tests": ["002.in"]})
