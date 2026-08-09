from __future__ import annotations

import threading
import time
import unittest
from typing import cast
from unittest.mock import patch

from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)
from app.service.verification.task_scheduler import (
    TaskExecutionResult,
    TaskPublishResult,
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
)
from app.service.verification.task_store import VerificationTaskStore


def _execution_result(
    verdict: str,
    *,
    feedback: str = "",
    error: str = "",
    output_ref: str = "",
) -> ExecutionResult:
    passes: tuple[ExecutionPassResult, ...] = ()
    if output_ref:
        placeholder_ref = "blob://sha256/" + ("0" * 64)
        passes = (
            ExecutionPassResult(
                number=1,
                capture_status=CAPTURE_COMPLETE,
                runresult="wrong-answer",
                verdict=verdict,
                score_text="",
                answer_correct=False,
                usage=ExecutionUsage(0.01, 0.01, 0.01, 1),
                feedback=feedback,
                artifacts=PassArtifacts(
                    input_ref=placeholder_ref,
                    output_ref=output_ref,
                    stderr_ref=placeholder_ref,
                    system_ref=placeholder_ref,
                    judge_message_ref=placeholder_ref,
                    team_message_ref=placeholder_ref,
                    metadata_ref=placeholder_ref,
                    compare_metadata_ref=placeholder_ref,
                ),
            ),
        )
    return normalize_execution_result(
        passes=passes,
        verdict=verdict,
        feedback=feedback,
        error=error,
    )


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
        "verification_id": "verification",
        "predecessor_task_id": "",
        "task_kind": task_kind,
        "source_path": source_path,
        "logical_run_id": logical_run_id or f"run-{task_id}",
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

class _InMemoryTaskStore:
    def __init__(self, rows: list[dict[str, object]], edges: list[tuple[str, str]]) -> None:
        self._rows = [dict(row) for row in rows]
        self._edges = list(edges)
        self._fail_flag = False
        self._fail_reason = ""
        self._lock = threading.Lock()
        self.list_rows_calls = 0
        self.saved_task_ids: list[str] = []
        self.save_batch_sizes: list[int] = []

    def list_rows(self, verification_id: str) -> list[dict[str, object]]:
        with self._lock:
            self.list_rows_calls += 1
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

    def requeue_leased_tasks(self, verification_id: str, judgehost_task_ids: list[str]) -> list[str]:
        allowed = set(judgehost_task_ids)
        changed: list[str] = []
        with self._lock:
            for row in self._rows:
                if (
                    str(row.get("verification_id") or "") == verification_id
                    and str(row.get("status") or "") == VerificationTaskStore.TASK_LEASED
                    and str(row.get("judgehost_task_id") or "") in allowed
                ):
                    row["status"] = VerificationTaskStore.TASK_QUEUED
                    row["started_at"] = ""
                    changed.append(str(row["id"]))
        return changed

    def save_task_result(
        self,
        task_id: str,
        *,
        status: str,
        run_id: str,
        judgehost_task_id: str,
        result: ExecutionResult,
    ) -> None:
        _ = run_id, judgehost_task_id
        with self._lock:
            for row in self._rows:
                if str(row["id"]) != task_id:
                    continue
                if str(row["status"]) == VerificationTaskStore.TASK_CANCELLED:
                    return
                row["status"] = status
                row["verdict"] = result.verdict
                row["runtime_sec"] = result.runtime_sec
                row["cpu_sec"] = result.cpu_sec
                row["wall_sec"] = result.wall_sec
                row["memory_kb"] = result.memory_kb
                row["compile_log"] = result.compile.log
                row["diagnostics_json"] = "[]"
                row["error_text"] = result.outcome.error
                row["feedback_text"] = result.outcome.feedback
                row["output_ref"] = result.output_run_ref
                row["answer_correct"] = result.outcome.answer_correct
                row["result"] = result
                self.saved_task_ids.append(task_id)
                return

    def save_task_results(self, results: list[dict[str, object]]) -> set[str]:
        self.save_batch_sizes.append(len(results))
        skipped_task_ids: set[str] = set()
        for result in results:
            execution_result = cast(ExecutionResult, result["result"])
            task_id = str(result["task_id"])
            self.save_task_result(
                task_id,
                status=str(result["status"]),
                run_id="",
                judgehost_task_id="",
                result=execution_result,
            )
            if (
                str(result["status"]) == VerificationTaskStore.TASK_DONE
                and execution_result.verdict.upper() == "SK"
            ):
                skipped_task_ids.add(task_id)
                stack = [task_id]
                while stack:
                    parent_id = stack.pop()
                    for edge_parent, child_id in self._edges:
                        if edge_parent != parent_id:
                            continue
                        stack.append(child_id)
                        for row in self._rows:
                            if str(row["id"]) != child_id:
                                continue
                            if str(row["status"]) == VerificationTaskStore.TASK_PENDING:
                                skipped = _execution_result(
                                    "SK",
                                    feedback="skipped because generate-input was skipped",
                                )
                                row["status"] = VerificationTaskStore.TASK_DONE
                                row["verdict"] = skipped.verdict
                                row["feedback_text"] = skipped.outcome.feedback
                                row["result"] = skipped
                                skipped_task_ids.add(child_id)
                            break
        return skipped_task_ids

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

class TestVerificationRuntimeCoordinator(unittest.TestCase):
    def _wait_until(
        self,
        predicate,
        *,
        timeout: float,
        interval: float,
        message: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        self.fail(message)

    def test_runtime_coordinator_publishes_successor_after_case_report_event(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                    logical_run_id="logical-generate",
                ),
                _task_row(
                    "vt-main",
                    task_kind="main-correct",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/main.cpp",
                    logical_run_id="logical-main",
                ),
            ],
            edges=[("vt-generate", "vt-main")],
        )
        publish_order: list[str] = []
        closed_logical_runs: list[str] = []
        final_result = TaskExecutionResult(
            task_id="vt-generate",
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-generate",
            judgehost_task_id="jt-generate",
            result=_execution_result("OK"),
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
            probe_task_case_cache=lambda _task_ids: set(),
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_execution=lambda _reason: None,
            close_logical_runs=closed_logical_runs.extend,
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
            self._wait_until(
                lambda: closed_logical_runs == ["logical-generate"],
                timeout=2.0,
                interval=0.01,
                message="durable logical run result did not close its execution batch",
            )
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_skips_entire_downstream_subtree_after_generate_skip(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                    logical_run_id="logical-generate",
                ),
                _task_row(
                    "vt-main",
                    task_kind="main-correct",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=2,
                    source_path="solutions/main.cpp",
                    logical_run_id="logical-main",
                ),
                _task_row(
                    "vt-solution",
                    task_kind="solution-run",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=3,
                    source_path="solutions/other.cpp",
                    logical_run_id="logical-solution",
                ),
            ],
            edges=[("vt-generate", "vt-main"), ("vt-main", "vt-solution")],
        )
        publish_order: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id="",
                judgehost_task_id="",
                terminal_result=TaskExecutionResult(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=_execution_result(
                        "SK",
                        feedback="duplicate generator invocation; skipped, same as 001.in",
                    ),
                ),
            )

        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime-skip-subtree",
            task_store=store,
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                resolve_case_result=lambda _task_id, _test_name: None,
                cancel_execution=lambda _reason: None,
                close_logical_runs=lambda _run_ids: None,
            ),
            edges=[("vt-generate", "vt-main"), ("vt-main", "vt-solution")],
        )

        coordinator.run()

        self.assertEqual(publish_order, ["vt-generate"])
        rows = {str(row["id"]): row for row in store.list_rows("ver-runtime-skip-subtree")}
        for task_id in ("vt-generate", "vt-main", "vt-solution"):
            self.assertEqual(str(rows[task_id]["status"]), VerificationTaskStore.TASK_DONE)
            self.assertEqual(str(rows[task_id]["verdict"]), "SK")

    def test_runtime_coordinator_actively_probes_cached_cases_after_identity_registration(self) -> None:
        total_tasks = 257
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
        probe_slices: list[list[str]] = []
        identity_registered: list[bool] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )

        coordinator: VerificationRuntimeCoordinator

        def _probe(task_ids: list[str]) -> set[str]:
            probe_slices.append(list(task_ids))
            rows_by_judgehost_id = {
                str(row["judgehost_task_id"]): row
                for row in store.list_rows("ver-large-batch")
                if row["judgehost_task_id"]
            }
            identity_registered.append(
                len(task_ids) <= 256
                and all(
                    task_id in rows_by_judgehost_id
                    and str(rows_by_judgehost_id[task_id]["status"])
                    == VerificationTaskStore.TASK_QUEUED
                    for task_id in task_ids
                )
            )
            for judgehost_task_id in task_ids:
                task_id = judgehost_task_id.removeprefix("jt-")
                index = int(task_id.removeprefix("vt-"))
                coordinator.enqueue_case_reported(
                    judgehost_task_id,
                    f"{index + 1:03}.in",
                    {
                        "final_result": TaskExecutionResult(
                            task_id=task_id,
                            status=VerificationTaskStore.TASK_DONE,
                            run_id=f"r-{task_id}",
                            judgehost_task_id=judgehost_task_id,
                            result=_execution_result("OK"),
                        )
                    },
                )
            return set()

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            probe_task_case_cache=_probe,
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_execution=lambda _reason: None,
            close_logical_runs=lambda _run_ids: None,
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
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive(), "active cache probes did not finish the graph")
            self.assertEqual(len(publish_order), total_tasks)
            self.assertEqual([len(task_ids) for task_ids in probe_slices], [256, 1])
            self.assertTrue(all(identity_registered))
            self.assertTrue(
                all(
                    str(row["status"]) == VerificationTaskStore.TASK_DONE
                    for row in store.list_rows("ver-large-batch")
                )
            )
            self.assertEqual(sum(store.save_batch_sizes), total_tasks)
            self.assertTrue(all(size <= 256 for size in store.save_batch_sizes))
            self.assertLess(len(store.save_batch_sizes), total_tasks)
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_requeues_leases_reported_by_expiry_reconciliation(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        "vt-expired",
                        task_kind="solution-run",
                        status=VerificationTaskStore.TASK_LEASED,
                        queue_index=1,
                        logical_run_id="logical-expired",
                    ),
                    "run_id": "r-expired",
                    "judgehost_task_id": "jt-expired",
                }
            ],
            edges=[],
        )
        reconcile_calls = 0

        def _reconcile() -> list[str]:
            nonlocal reconcile_calls
            reconcile_calls += 1
            return ["jt-expired"] if reconcile_calls == 1 else []

        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda _row: (_ for _ in ()).throw(RuntimeError("unexpected publish")),
            probe_task_case_cache=lambda _task_ids: set(),
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_execution=lambda _reason: None,
            close_logical_runs=lambda _run_ids: None,
            reconcile_expired_leases=_reconcile,
        )
        coordinator = VerificationRuntimeCoordinator(
            "verification",
            task_store=store,
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: str(store.list_rows("verification")[0]["status"])
                == VerificationTaskStore.TASK_QUEUED,
                timeout=2.0,
                interval=0.01,
                message="expired lease was not requeued",
            )
            self.assertGreaterEqual(reconcile_calls, 1)
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

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
            run_id="r-generate",
            judgehost_task_id="jt-vt-generate",
            result=_execution_result(
                "WA",
                error="validator rejected generated input for 001.in",
                feedback="validator rejected generated input for 001.in",
                output_ref="blob://sha256/" + ("1" * 64),
            ),
            fail_flag_reason="generate-input / generators/gen.cpp / 001.in: validator rejected generated input for 001.in",
        )

        def _cancel_execution(reason: str) -> None:
            store.cancel_not_started_tasks("ver-validator-stop", reason=reason)

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
            probe_task_case_cache=lambda _task_ids: set(),
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_execution=_cancel_execution,
            close_logical_runs=lambda _run_ids: None,
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
            probe_task_case_cache=lambda _task_ids: set(),
            resolve_case_result=lambda _task_id, _test_name: None,
            cancel_execution=lambda reason: queued_cancel_reasons.append(reason),
            close_logical_runs=lambda _run_ids: None,
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
            self.assertEqual(queued_cancel_reasons, ["verification cancelled by user"])
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_indexes_rows_once_for_an_incremental_dag(self) -> None:
        task_count = 64
        task_ids = [f"vt-{index:03}" for index in range(task_count)]
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    task_id,
                    task_kind="solution-run",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=index,
                    test_name=f"{index:03}.in",
                )
                for index, task_id in enumerate(task_ids, start=1)
            ],
            edges=list(zip(task_ids, task_ids[1:])),
        )
        publish_order: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"run-{task_id}",
                judgehost_task_id=f"judgehost-{task_id}",
                terminal_result=TaskExecutionResult(
                    task_id=task_id,
                    status=VerificationTaskStore.TASK_DONE,
                    run_id=f"run-{task_id}",
                    judgehost_task_id=f"judgehost-{task_id}",
                    result=_execution_result("AC"),
                ),
            )

        coordinator = VerificationRuntimeCoordinator(
            "ver-incremental-chain",
            task_store=store,
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                resolve_case_result=lambda _task_id, _test_name: None,
                cancel_execution=lambda _reason: None,
                close_logical_runs=lambda _run_ids: None,
            ),
            edges=list(zip(task_ids, task_ids[1:])),
        )

        coordinator.run()

        self.assertEqual(publish_order, task_ids)
        self.assertEqual(store.saved_task_ids, task_ids)
        self.assertEqual(store.list_rows_calls, 1)

    def test_task_terminal_compensation_is_idempotent_and_unlocks_each_edge_once(self) -> None:
        verification_id = "ver-terminal-compensation"
        rows = [
            {
                **_task_row(
                    task_id,
                    task_kind="solution-run",
                    status=VerificationTaskStore.TASK_QUEUED,
                    queue_index=index,
                    test_name=f"{index:03}.in",
                ),
                "verification_id": verification_id,
                "run_id": "run-shared",
                "judgehost_task_id": "judgehost-shared",
            }
            for index, task_id in enumerate(("vt-parent-a", "vt-parent-b"), start=1)
        ]
        rows.append(
            {
                **_task_row(
                    "vt-child",
                    task_kind="solution-run",
                    status=VerificationTaskStore.TASK_PENDING,
                    queue_index=3,
                    test_name="003.in",
                ),
                "verification_id": verification_id,
            }
        )
        store = _InMemoryTaskStore(
            rows=rows,
            edges=[("vt-parent-a", "vt-child"), ("vt-parent-b", "vt-child")],
        )
        published: list[str] = []
        finalized: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            published.append(task_id)
            return TaskPublishResult(task_id, f"run-{task_id}", f"judgehost-{task_id}")

        def _finalize(row: dict[str, object], *, result: dict[str, object]) -> TaskExecutionResult:
            task_id = str(row["id"])
            finalized.append(task_id)
            return TaskExecutionResult(
                task_id=task_id,
                status=VerificationTaskStore.TASK_DONE,
                run_id=str(row["run_id"]),
                judgehost_task_id=str(row["judgehost_task_id"]),
                result=_execution_result("AC"),
            )

        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=store,
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                resolve_case_result=lambda _task_id, _test_name: {"status": "ok"},
                cancel_execution=lambda _reason: None,
                close_logical_runs=lambda _run_ids: None,
            ),
            edges=[("vt-parent-a", "vt-child"), ("vt-parent-b", "vt-child")],
        )
        with patch(
            "app.service.verification.task_result_finalize.finalize_verification_task_result",
            side_effect=_finalize,
        ):
            self.assertTrue(coordinator._finalize_terminal_task("judgehost-shared"))
            self.assertTrue(coordinator._publish_ready_rows())
            self.assertFalse(coordinator._finalize_terminal_task("judgehost-shared"))
            self.assertFalse(coordinator._publish_ready_rows())

        self.assertEqual(finalized, ["vt-parent-a", "vt-parent-b"])
        self.assertEqual(store.saved_task_ids, ["vt-parent-a", "vt-parent-b"])
        self.assertEqual(published, ["vt-child"])
        self.assertEqual(store.list_rows_calls, 1)
