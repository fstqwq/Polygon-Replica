from __future__ import annotations

import json
import threading
import time

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

from .common import SmokeBase, config
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

    def list_edge_rows(self, verification_id: str) -> list[dict[str, str]]:
        return [dict(row) for row in self._edges]

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

    def requeue_task(self, task_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["id"]) == task_id and str(row["status"]) == VerificationTaskStore.TASK_LEASED:
                    row["status"] = VerificationTaskStore.TASK_QUEUED
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
        compile_log_truncated: bool,
        compile_log_total_chars: int,
        diagnostics_json: str,
        diagnostics_truncated: bool,
        diagnostics_total: int,
        error_text: str,
        error_text_truncated: bool,
        error_text_total_chars: int,
        feedback_text: str,
        feedback_text_truncated: bool,
        feedback_text_total_chars: int,
        output_ref: str,
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
            metadata={"status": "running"},
        )

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
        self.assertEqual(status, "failed")
        self.assertTrue(finished)

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
            compile_log_truncated=False,
            compile_log_total_chars=0,
            diagnostics_json="[]",
            diagnostics_truncated=False,
            diagnostics_total=0,
            error_text="",
            error_text_truncated=False,
            error_text_total_chars=0,
            feedback_text="",
            feedback_text_truncated=False,
            feedback_text_total_chars=0,
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

    def test_runtime_coordinator_cancel_keeps_leased_rows_until_reported(self) -> None:
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
        final_result = TaskExecutionResult(
            task_id="vt-leased",
            status=VerificationTaskStore.TASK_DONE,
            verdict="OK",
            run_id="r-a",
            judgehost_task_id="jt-leased",
            runtime_sec=0.01,
            cpu_sec=0.01,
            wall_sec=0.01,
            memory_kb=1,
            compile_log="",
            compile_log_truncated=False,
            compile_log_total_chars=0,
            diagnostics_json="[]",
            diagnostics_truncated=False,
            diagnostics_total=0,
            error_text="",
            error_text_truncated=False,
            error_text_total_chars=0,
            feedback_text="",
            feedback_text_truncated=False,
            feedback_text_total_chars=0,
            output_ref="",
        )

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

            coordinator.enqueue_case_reported(
                "jt-leased",
                "001.in",
                {"final_result": final_result},
            )
            self._wait_until(
                lambda: str({str(row["id"]): row for row in store.list_rows("ver-runtime-cancel")}["vt-leased"]["status"]) == VerificationTaskStore.TASK_DONE,
                timeout=2.0,
                interval=0.01,
                message="leased row was not finalized after case report",
            )
        finally:
            if thread.is_alive():
                coordinator.enqueue_case_reported(
                    "jt-leased",
                    "001.in",
                    {"final_result": final_result},
                )
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
            compile_log_truncated=False,
            compile_log_total_chars=0,
            diagnostics_json="[]",
            diagnostics_truncated=False,
            diagnostics_total=0,
            error_text="",
            error_text_truncated=False,
            error_text_total_chars=0,
            feedback_text="",
            feedback_text_truncated=False,
            feedback_text_total_chars=0,
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
                "compile_log_truncated": 0,
                "compile_log_total_chars": 0,
                "diagnostics_json": "[]",
                "diagnostics_truncated": 0,
                "diagnostics_total": 0,
                "error_text": "",
                "error_text_truncated": 0,
                "error_text_total_chars": 0,
                "feedback_text": "",
                "feedback_text_truncated": 0,
                "feedback_text_total_chars": 0,
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

    def test_startup_cancel_task_graph_verifications_reconciles_stale_rows(self) -> None:
        from app.impl.auth.internal.runtime import _startup_cancel_task_graph_verifications

        verification_id = "ver-startup-reconcile"
        self._insert_verification_row(verification_id)
        config.verification_service.persist_verification_metadata(
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
                "compile_log_truncated": 0,
                "compile_log_total_chars": 0,
                "diagnostics_json": "[]",
                "diagnostics_truncated": 0,
                "diagnostics_total": 0,
                "error_text": "",
                "error_text_truncated": 0,
                "error_text_total_chars": 0,
                "feedback_text": "",
                "feedback_text_truncated": 0,
                "feedback_text_total_chars": 0,
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
                "compile_log_truncated": 0,
                "compile_log_total_chars": 0,
                "diagnostics_json": "[]",
                "diagnostics_truncated": 0,
                "diagnostics_total": 0,
                "error_text": "",
                "error_text_truncated": 0,
                "error_text_total_chars": 0,
                "feedback_text": "",
                "feedback_text_truncated": 0,
                "feedback_text_total_chars": 0,
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
        self.assertEqual(summary.get("execution_model"), "task-dag")
        self.assertEqual(summary.get("runs_order"), ["r-main", "r-wa"])
        self.assertIn("r-main", summary.get("runs") or {})
        self.assertEqual([str(item.get("source_path") or "") for item in summary.get("solutions") or []], ["solutions/wa.cpp"])

    def test_summary_parts_uses_canonical_metadata_shapes(self) -> None:
        from app.impl.workspace.verification_dag import _summary_parts

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
        self.assertGreaterEqual(parts.compile_log_total_chars, len("compile failed"))
        self.assertEqual(parts.diagnostics_total, 1)
        self.assertTrue(bool(diagnostics_rows[0].get("message_truncated")))
        self.assertEqual(parts.memory_kb, 123)
        self.assertAlmostEqual(parts.runtime_sec or 0.0, 0.01, places=3)

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
        self.assertTrue(str(compile_meta["text"]).startswith("xxxxxxxx"))
        self.assertTrue(bool(compile_meta["truncated"]))
        self.assertEqual(int(diagnostics_meta["total"]), 1)
        self.assertTrue(bool(diagnostics_meta["rows"][0].get("message_truncated")))
        self.assertIn('"message":"aaaaaaaaaa... [truncated; showing first 10 characters]"', diagnostics_json)

    def test_task_store_persists_fail_flag(self) -> None:
        verification_id = f"ver-task-store-{self.test_id}"
        self._insert_verification_row(verification_id)
        store = VerificationTaskStore(config.db)
        store.set_fail_flag(verification_id, reason="main failed")
        self.assertEqual(store.fail_state(verification_id), (True, "main failed"))
        self.assertIsNotNone(VerificationStore(config.db).record_row(verification_id))
