import threading
import time
import unittest
from unittest.mock import patch

from app.service.execution.model import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import normalize_execution_result
from app.service.verification.task_completion import CompletionCommit, TaskCompletion
from app.service.verification.task_scheduler import (
    TaskPublishResult,
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
)
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.types import VerificationTaskStatus


class _RegistryHandle:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def enqueue_case_leased(self, verification_task_id: str) -> None:
        self.events.append(("leased", verification_task_id))

    def enqueue_completion_committed(self, commit: CompletionCommit) -> None:
        self.events.append(("completion", commit))

    def enqueue_completion_reconciliation(self, commit: CompletionCommit) -> None:
        self.events.append(("reconcile", commit))

    def enqueue_cancel(self, reason: str) -> None:
        self.events.append(("cancel", reason))

    def enqueue_closed(self) -> None:
        self.events.append(("closed", ""))


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
    status: VerificationTaskStatus,
    queue_index: int,
    source_path: str = "solutions/a.cpp",
    program_id: str = "",
    test_name: str = "001.in",
) -> dict[str, object]:
    if not program_id:
        program_id = {
            "generate-input": "generator-0",
            "main-correct": "accepted",
            "solution-run": "solution-0",
        }[task_kind]
    return {
        "id": task_id,
        "verification_id": "verification",
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


class _FakeCompletionService:
    """Keep coordinator tests focused on committed task transitions."""

    def __init__(self, task_store: "_InMemoryTaskStore") -> None:
        self._task_store = task_store

    def commit(
        self,
        completions: list[TaskCompletion] | tuple[TaskCompletion, ...],
        *,
        notify: bool = True,
    ) -> CompletionCommit:
        _ = notify
        return self._task_store.commit_task_completions(completions)


class _InMemoryTaskStore:
    def __init__(self, rows: list[dict[str, object]], edges: list[tuple[str, str]]) -> None:
        self._rows = [dict(row) for row in rows]
        self._edges = list(edges)
        self._fail_flag = False
        self._fail_reason = ""
        self._lock = threading.Lock()
        self._verification_id = ""

    def list_rows(self, verification_id: str) -> list[dict[str, object]]:
        with self._lock:
            self._verification_id = verification_id
            return [
                dict(row)
                for row in sorted(
                    self._rows,
                    key=lambda item: (
                        int(item["queue_index"]),
                        str(item["id"]),
                    ),
                )
            ]

    def verification_is_running(self, verification_id: str) -> bool:
        _ = verification_id
        with self._lock:
            return not self._fail_flag

    def set_task_queued(self, task_id: str, *, run_id: str, judgehost_task_id: str) -> None:
        with self._lock:
            for row in self._rows:
                if (
                    str(row["id"]) == task_id
                    and str(row["status"]) == VerificationTaskStatus.PENDING
                ):
                    row["status"] = VerificationTaskStatus.QUEUED
                    row["run_id"] = run_id
                    row["judgehost_task_id"] = judgehost_task_id
                    return

    def set_task_leased(self, task_id: str) -> bool:
        with self._lock:
            for row in self._rows:
                if (
                    str(row["id"]) == task_id
                    and str(row["status"]) == VerificationTaskStatus.QUEUED
                ):
                    row["status"] = VerificationTaskStatus.LEASED
                    return True
        return False

    def requeue_leased_tasks(self, verification_id: str, judgehost_task_ids: list[str]) -> list[str]:
        allowed = set(judgehost_task_ids)
        changed: list[str] = []
        with self._lock:
            for row in self._rows:
                if (
                    str(row.get("verification_id") or "") == verification_id
                    and str(row.get("status") or "") == VerificationTaskStatus.LEASED
                    and str(row.get("judgehost_task_id") or "") in allowed
                ):
                    row["status"] = VerificationTaskStatus.QUEUED
                    row["started_at"] = ""
                    changed.append(str(row["id"]))
        return changed

    def commit_task_completions(
        self,
        completions: list[TaskCompletion] | tuple[TaskCompletion, ...],
    ) -> CompletionCommit:
        effective: list[TaskCompletion] = []
        committed_task_ids: set[str] = set()
        already_terminal_task_ids: set[str] = set()
        skipped_task_ids: set[str] = set()
        cancelled_task_ids: set[str] = set()
        failure_reason = ""
        terminal_statuses = {
            VerificationTaskStatus.DONE,
            VerificationTaskStatus.FAILED,
            VerificationTaskStatus.CANCELLED,
        }
        with self._lock:
            rows_by_id = {str(row["id"]): row for row in self._rows}
            for completion in completions:
                row = rows_by_id[completion.task_id]
                if str(row["status"]) in terminal_statuses:
                    already_terminal_task_ids.add(completion.task_id)
                    continue
                result = completion.result
                row["status"] = completion.status
                row["run_id"] = completion.run_id
                row["judgehost_task_id"] = completion.judgehost_task_id
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
                effective.append(completion)
                committed_task_ids.add(completion.task_id)
                if completion.fail_reason and not self._fail_flag:
                    self._fail_flag = True
                    self._fail_reason = completion.fail_reason
                    failure_reason = completion.fail_reason
                if (
                    completion.status == VerificationTaskStatus.DONE
                    and result.verdict.upper() == "SK"
                ):
                    skipped_task_ids.add(completion.task_id)
                    stack = [completion.task_id]
                    while stack:
                        parent_id = stack.pop()
                        for edge_parent, child_id in self._edges:
                            if edge_parent != parent_id:
                                continue
                            stack.append(child_id)
                            child = rows_by_id[child_id]
                            if str(child["status"]) != VerificationTaskStatus.PENDING:
                                continue
                            skipped = _execution_result(
                                "SK",
                                feedback="skipped because generate-input was skipped",
                            )
                            child["status"] = VerificationTaskStatus.DONE
                            child["verdict"] = skipped.verdict
                            child["feedback_text"] = skipped.outcome.feedback
                            child["result"] = skipped
                            skipped_task_ids.add(child_id)
            if failure_reason:
                for row in self._rows:
                    if str(row["status"]) not in {
                        VerificationTaskStatus.PENDING,
                        VerificationTaskStatus.QUEUED,
                    }:
                        continue
                    row["status"] = VerificationTaskStatus.CANCELLED
                    row["cancel_reason"] = failure_reason
                    cancelled_task_ids.add(str(row["id"]))
        return CompletionCommit(
            verification_id=self._verification_id,
            effective_completions=tuple(effective),
            committed_task_ids=frozenset(committed_task_ids),
            already_terminal_task_ids=frozenset(already_terminal_task_ids),
            skipped_task_ids=frozenset(skipped_task_ids),
            cancelled_task_ids=frozenset(cancelled_task_ids),
            parent_transition="failed" if failure_reason else "",
            failure_reason=failure_reason,
        )

    def cancel_open_tasks(self, verification_id: str, *, reason: str) -> None:
        with self._lock:
            for row in self._rows:
                if str(row["status"]) in {
                    VerificationTaskStatus.PENDING,
                    VerificationTaskStatus.QUEUED,
                }:
                    row["status"] = VerificationTaskStatus.CANCELLED
                    row["cancel_reason"] = reason

    def failure_snapshot(self) -> tuple[bool, str]:
        return (self._fail_flag, self._fail_reason)


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

    def test_runtime_coordinator_publishes_successor_after_completion_commit(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                    program_id="generator-0",
                ),
                _task_row(
                    "vt-main",
                    task_kind="main-correct",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=2,
                    source_path="solutions/main.cpp",
                    program_id="accepted",
                ),
            ],
            edges=[("vt-generate", "vt-main")],
        )
        publish_order: list[str] = []
        closed_programs: list[str] = []
        completion = TaskCompletion(
            task_id="vt-generate",
            status=VerificationTaskStatus.DONE,
            run_id="r-vt-generate",
            judgehost_task_id="jt-vt-generate",
            result=_execution_result("OK"),
        )

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            store.set_task_queued(
                task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            probe_task_case_cache=lambda _task_ids: set(),
            cancel_execution=lambda _reason: None,
            close_programs=closed_programs.extend,
        )
        completion_service = _FakeCompletionService(store)
        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime",
            task_store=store,
            completion_service=completion_service,
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
            self.assertEqual(
                store.list_rows("ver-runtime")[0]["status"],
                VerificationTaskStatus.QUEUED,
            )
            coordinator.enqueue_completion_committed(
                completion_service.commit([completion], notify=False)
            )
            self._wait_until(
                lambda: publish_order == ["vt-generate", "vt-main"],
                timeout=2.0,
                interval=0.01,
                message="successor task was not published after case result event",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-runtime")}
            self.assertEqual(str(rows["vt-generate"]["status"]), VerificationTaskStatus.DONE)
            self.assertEqual(str(rows["vt-main"]["status"]), VerificationTaskStatus.QUEUED)
            self._wait_until(
                lambda: closed_programs == ["generator-0"],
                timeout=2.0,
                interval=0.01,
                message="durable program result did not close its execution batch",
            )
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_runtime_coordinator_reconciles_persisted_completion_after_event_failure(
        self,
    ) -> None:
        verification_id = "ver-completion-reconciliation"
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        "vt-parent",
                        task_kind="generate-input",
                        status=VerificationTaskStatus.PENDING,
                        queue_index=1,
                    ),
                    "verification_id": verification_id,
                },
                {
                    **_task_row(
                        "vt-child",
                        task_kind="main-correct",
                        status=VerificationTaskStatus.PENDING,
                        queue_index=2,
                    ),
                    "verification_id": verification_id,
                },
            ],
            edges=[("vt-parent", "vt-child")],
        )
        published: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            published.append(task_id)
            store.set_task_queued(
                task_id,
                run_id=f"run-{task_id}",
                judgehost_task_id=f"judgehost-{task_id}",
            )
            return TaskPublishResult(
                task_id,
                f"run-{task_id}",
                f"judgehost-{task_id}",
            )

        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=lambda _reason: None,
                close_programs=lambda _program_ids: None,
            ),
            edges=[("vt-parent", "vt-child")],
        )
        registry = VerificationRuntimeRegistry()

        class FailingCompletionHandle(_RegistryHandle):
            def enqueue_completion_committed(self, commit: CompletionCommit) -> None:
                del commit
                raise RuntimeError("completion event unavailable")

            def enqueue_completion_reconciliation(
                self,
                commit: CompletionCommit,
            ) -> None:
                coordinator.enqueue_completion_reconciliation(commit)

        handle = FailingCompletionHandle()
        registry.register(verification_id, handle)
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: published == ["vt-parent"],
                timeout=2.0,
                interval=0.01,
                message="parent task was not published",
            )
            commit = store.commit_task_completions(
                [
                    TaskCompletion(
                        task_id="vt-parent",
                        status=VerificationTaskStatus.DONE,
                        run_id="run-vt-parent",
                        judgehost_task_id="judgehost-vt-parent",
                        result=_execution_result("OK"),
                    )
                ]
            )
            self.assertTrue(registry.completion_committed(verification_id, commit))
            self._wait_until(
                lambda: published == ["vt-parent", "vt-child"],
                timeout=2.0,
                interval=0.01,
                message="durable completion reconciliation did not publish successor",
            )
        finally:
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertTrue(registry.unregister(verification_id, handle))

    def test_terminal_idle_reconciliation_drains_when_event_delivery_fails(
        self,
    ) -> None:
        verification_id = "ver-terminal-delivery-failure"
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        "vt-parent",
                        task_kind="generate-input",
                        status=VerificationTaskStatus.PENDING,
                        queue_index=1,
                    ),
                    "verification_id": verification_id,
                },
                {
                    **_task_row(
                        "vt-child",
                        task_kind="main-correct",
                        status=VerificationTaskStatus.PENDING,
                        queue_index=2,
                    ),
                    "verification_id": verification_id,
                },
            ],
            edges=[("vt-parent", "vt-child")],
        )
        published = threading.Event()
        drained = threading.Event()
        drain_reasons: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            store.set_task_queued(
                task_id,
                run_id=f"run-{task_id}",
                judgehost_task_id=f"judgehost-{task_id}",
            )
            published.set()
            return TaskPublishResult(
                task_id,
                f"run-{task_id}",
                f"judgehost-{task_id}",
            )

        def _drain(reason: str) -> None:
            drain_reasons.append(reason)
            drained.set()

        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=_drain,
                close_programs=lambda _program_ids: None,
            ),
            edges=[("vt-parent", "vt-child")],
        )
        registry = VerificationRuntimeRegistry()

        class FailedDeliveryHandle(_RegistryHandle):
            def enqueue_completion_committed(self, commit: CompletionCommit) -> None:
                del commit
                raise RuntimeError("completion event unavailable")

            def enqueue_completion_reconciliation(
                self,
                commit: CompletionCommit,
            ) -> None:
                del commit
                raise RuntimeError("durable reconciliation unavailable")

        handle = FailedDeliveryHandle()
        registry.register(verification_id, handle)
        with patch(
            "app.service.verification.task_scheduler._IDLE_RECONCILIATION_SEC",
            0.05,
        ):
            thread = threading.Thread(target=coordinator.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(published.wait(timeout=2.0))
                coordinator.enqueue_case_leased("vt-parent")
                self._wait_until(
                    lambda: str(store.list_rows(verification_id)[0]["status"])
                    == VerificationTaskStatus.LEASED,
                    timeout=2.0,
                    interval=0.01,
                    message="parent task was not leased before terminal failure",
                )
                commit = store.commit_task_completions(
                    [
                        TaskCompletion(
                            task_id="vt-parent",
                            status=VerificationTaskStatus.FAILED,
                            run_id="run-vt-parent",
                            judgehost_task_id="judgehost-vt-parent",
                            result=_execution_result(
                                "FL",
                                error="generator infrastructure failure",
                            ),
                            fail_reason="generator infrastructure failure",
                        )
                    ]
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "completion event unavailable; durable reconciliation unavailable",
                ) as raised:
                    registry.completion_committed(verification_id, commit)
                self.assertEqual(
                    str(raised.exception.__cause__),
                    "completion event unavailable",
                )
                self.assertTrue(
                    drained.wait(timeout=2.0),
                    "terminal durable state did not drain Judgehost execution",
                )
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
                self.assertEqual(
                    drain_reasons,
                    ["verification is no longer running"],
                )
            finally:
                if thread.is_alive():
                    coordinator.enqueue_cancel("test shutdown")
                    thread.join(timeout=2.0)
        self.assertTrue(registry.unregister(verification_id, handle))

    def test_runtime_coordinator_skips_entire_downstream_subtree_after_generate_skip(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    "vt-generate",
                    task_kind="generate-input",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                    program_id="generator-0",
                ),
                _task_row(
                    "vt-main",
                    task_kind="main-correct",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=2,
                    source_path="solutions/main.cpp",
                    program_id="accepted",
                ),
                _task_row(
                    "vt-solution",
                    task_kind="solution-run",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=3,
                    source_path="solutions/other.cpp",
                    program_id="solution-0",
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
                terminal_result=TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
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
            completion_service=_FakeCompletionService(store),
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=lambda _reason: None,
                close_programs=lambda _program_ids: None,
            ),
            edges=[("vt-generate", "vt-main"), ("vt-main", "vt-solution")],
        )

        coordinator.run()

        self.assertEqual(publish_order, ["vt-generate"])
        rows = {str(row["id"]): row for row in store.list_rows("ver-runtime-skip-subtree")}
        for task_id in ("vt-generate", "vt-main", "vt-solution"):
            self.assertEqual(str(rows[task_id]["status"]), VerificationTaskStatus.DONE)
            self.assertEqual(str(rows[task_id]["verdict"]), "SK")

    def test_runtime_coordinator_actively_probes_cached_cases_after_identity_registration(self) -> None:
        total_tasks = 257
        store = _InMemoryTaskStore(
            rows=[
                _task_row(
                    f"vt-{index:03}",
                    task_kind="generate-input",
                    status=VerificationTaskStatus.PENDING,
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
        completion_service = _FakeCompletionService(store)

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            store.set_task_queued(
                task_id,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
            )
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
                    == VerificationTaskStatus.QUEUED
                    for task_id in task_ids
                )
            )
            for judgehost_task_id in task_ids:
                task_id = judgehost_task_id.removeprefix("jt-")
                commit = completion_service.commit(
                    [
                        TaskCompletion(
                            task_id=task_id,
                            status=VerificationTaskStatus.DONE,
                            run_id=f"r-{task_id}",
                            judgehost_task_id=judgehost_task_id,
                            result=_execution_result("OK"),
                        )
                    ],
                    notify=False,
                )
                coordinator.enqueue_completion_committed(commit)
            return set()

        callbacks = VerificationRuntimeCallbacks(
            publish_task=_publish,
            probe_task_case_cache=_probe,
            cancel_execution=lambda _reason: None,
            close_programs=lambda _program_ids: None,
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-large-batch",
            task_store=store,
            completion_service=completion_service,
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
                    str(row["status"]) == VerificationTaskStatus.DONE
                    for row in store.list_rows("ver-large-batch")
                )
            )
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
                        status=VerificationTaskStatus.LEASED,
                        queue_index=1,
                        program_id="solution-0",
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
            cancel_execution=lambda _reason: None,
            close_programs=lambda _program_ids: None,
            reconcile_expired_leases=_reconcile,
        )
        coordinator = VerificationRuntimeCoordinator(
            "verification",
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            self._wait_until(
                lambda: str(store.list_rows("verification")[0]["status"])
                == VerificationTaskStatus.QUEUED,
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
                    status=VerificationTaskStatus.PENDING,
                    queue_index=1,
                    source_path="generators/gen.cpp",
                ),
                _task_row(
                    "vt-solution",
                    task_kind="solution-run",
                    status=VerificationTaskStatus.PENDING,
                    queue_index=2,
                    source_path="solutions/ok.cpp",
                    program_id="solution-0",
                ),
            ],
            edges=[("vt-generate", "vt-solution")],
        )
        publish_order: list[str] = []
        completion = TaskCompletion(
            task_id="vt-generate",
            status=VerificationTaskStatus.FAILED,
            run_id="r-generate",
            judgehost_task_id="jt-vt-generate",
            result=_execution_result(
                "WA",
                error="validator rejected generated input for 001.in",
                feedback="validator rejected generated input for 001.in",
                output_ref="blob://sha256/" + ("1" * 64),
            ),
            fail_reason="generate-input / generators/gen.cpp / 001.in: validator rejected generated input for 001.in",
        )

        def _cancel_execution(reason: str) -> None:
            store.cancel_open_tasks("ver-validator-stop", reason=reason)

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
            cancel_execution=_cancel_execution,
            close_programs=lambda _program_ids: None,
        )
        completion_service = _FakeCompletionService(store)
        coordinator = VerificationRuntimeCoordinator(
            "ver-validator-stop",
            task_store=store,
            completion_service=completion_service,
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
            coordinator.enqueue_completion_committed(
                completion_service.commit([completion], notify=False)
            )
            self._wait_until(
                lambda: store.failure_snapshot()[0],
                timeout=2.0,
                interval=0.01,
                message="validator rejection did not set fail flag",
            )
            self._wait_until(
                lambda: str(
                    {
                        str(row["id"]): row
                        for row in store.list_rows("ver-validator-stop")
                    }["vt-solution"]["status"]
                )
                == VerificationTaskStatus.CANCELLED,
                timeout=2.0,
                interval=0.01,
                message="successor task was not cancelled after validator rejection",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-validator-stop")}
            self.assertEqual(str(rows["vt-generate"]["status"]), VerificationTaskStatus.FAILED)
            self.assertEqual(str(rows["vt-solution"]["status"]), VerificationTaskStatus.CANCELLED)
            self.assertEqual(publish_order, ["vt-generate"])
            self.assertEqual(
                store.failure_snapshot(),
                (True, "generate-input / generators/gen.cpp / 001.in: validator rejected generated input for 001.in"),
            )
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_synchronous_failure_stops_independent_root_publication(self) -> None:
        verification_id = "ver-synchronous-failure"
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        task_id,
                        task_kind="solution-run",
                        status=VerificationTaskStatus.PENDING,
                        queue_index=index,
                        program_id=f"solution-{index}",
                        test_name="001.in",
                    ),
                    "verification_id": verification_id,
                }
                for index, task_id in enumerate(
                    ("vt-first", "vt-independent"),
                    start=1,
                )
            ],
            edges=[],
        )
        publish_order: list[str] = []
        drain_reasons: list[str] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            publish_order.append(task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id=f"run-{task_id}",
                judgehost_task_id=f"judgehost-{task_id}",
                terminal_result=TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.FAILED,
                    run_id=f"run-{task_id}",
                    judgehost_task_id=f"judgehost-{task_id}",
                    result=_execution_result(
                        "FL",
                        error="source payload is unavailable",
                    ),
                    fail_reason="source payload is unavailable",
                ),
            )

        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=drain_reasons.append,
                close_programs=lambda _program_ids: None,
            ),
            edges=[],
        )

        coordinator.run()

        rows = {
            str(row["id"]): row
            for row in store.list_rows(verification_id)
        }
        self.assertEqual(publish_order, ["vt-first"])
        self.assertEqual(drain_reasons, ["source payload is unavailable"])
        self.assertEqual(
            str(rows["vt-independent"]["status"]),
            VerificationTaskStatus.CANCELLED,
        )

    def test_runtime_coordinator_cancel_releases_worker_without_finalizing_leased_rows(self) -> None:
        store = _InMemoryTaskStore(
            rows=[
                {
                    **_task_row(
                        "vt-leased",
                        task_kind="solution-run",
                        status=VerificationTaskStatus.LEASED,
                        queue_index=1,
                        program_id="solution-0",
                        test_name="001.in",
                    ),
                    "run_id": "r-a",
                    "judgehost_task_id": "jt-leased",
                },
                {
                    **_task_row(
                        "vt-queued",
                        task_kind="solution-run",
                        status=VerificationTaskStatus.QUEUED,
                        queue_index=2,
                        program_id="solution-0",
                        test_name="002.in",
                    ),
                    "run_id": "r-b",
                    "judgehost_task_id": "jt-queued",
                },
            ],
            edges=[],
        )
        callbacks = VerificationRuntimeCallbacks(
            publish_task=lambda _row: (_ for _ in ()).throw(RuntimeError("unexpected publish")),
            probe_task_case_cache=lambda _task_ids: set(),
            cancel_execution=lambda _reason: None,
            close_programs=lambda _program_ids: None,
        )
        coordinator = VerificationRuntimeCoordinator(
            "ver-runtime-cancel",
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=callbacks,
            edges=[],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            coordinator.enqueue_cancel("verification cancelled by user")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            store.cancel_open_tasks(
                "ver-runtime-cancel",
                reason="verification cancelled by user",
            )
            self._wait_until(
                lambda: str(
                    {
                        str(row["id"]): row
                        for row in store.list_rows("ver-runtime-cancel")
                    }["vt-queued"]["status"]
                )
                == VerificationTaskStatus.CANCELLED,
                timeout=2.0,
                interval=0.01,
                message="queued row was not cancelled",
            )
            rows = {str(row["id"]): row for row in store.list_rows("ver-runtime-cancel")}
            self.assertEqual(str(rows["vt-leased"]["status"]), VerificationTaskStatus.LEASED)
            self.assertEqual(str(rows["vt-queued"]["status"]), VerificationTaskStatus.CANCELLED)
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
                    status=VerificationTaskStatus.PENDING,
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
                terminal_result=TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id=f"run-{task_id}",
                    judgehost_task_id=f"judgehost-{task_id}",
                    result=_execution_result("AC"),
                ),
            )

        coordinator = VerificationRuntimeCoordinator(
            "ver-incremental-chain",
            task_store=store,
            completion_service=_FakeCompletionService(store),
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=lambda _reason: None,
                close_programs=lambda _program_ids: None,
            ),
            edges=list(zip(task_ids, task_ids[1:])),
        )

        coordinator.run()

        self.assertEqual(publish_order, task_ids)
        self.assertTrue(
            all(
                str(row["status"]) == VerificationTaskStatus.DONE
                for row in store.list_rows("ver-incremental-chain")
            )
        )

    def test_task_terminal_compensation_is_idempotent_and_unlocks_each_edge_once(self) -> None:
        verification_id = "ver-terminal-compensation"
        rows = [
            {
                **_task_row(
                    task_id,
                    task_kind="solution-run",
                    status=VerificationTaskStatus.QUEUED,
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
                    status=VerificationTaskStatus.PENDING,
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
        completion_service = _FakeCompletionService(store)
        completions = [
            TaskCompletion(
                task_id=task_id,
                status=VerificationTaskStatus.DONE,
                run_id="run-shared",
                judgehost_task_id="judgehost-shared",
                result=_execution_result("AC"),
            )
            for task_id in ("vt-parent-a", "vt-parent-b")
        ]

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            task_id = str(row["id"])
            published.append(task_id)
            return TaskPublishResult(task_id, f"run-{task_id}", f"judgehost-{task_id}")

        coordinator = VerificationRuntimeCoordinator(
            verification_id,
            task_store=store,
            completion_service=completion_service,
            callbacks=VerificationRuntimeCallbacks(
                publish_task=_publish,
                probe_task_case_cache=lambda _task_ids: set(),
                cancel_execution=lambda _reason: None,
                close_programs=lambda _program_ids: None,
            ),
            edges=[("vt-parent-a", "vt-child"), ("vt-parent-b", "vt-child")],
        )
        thread = threading.Thread(target=coordinator.run, daemon=True)
        thread.start()
        try:
            coordinator.enqueue_completion_committed(
                completion_service.commit(completions, notify=False)
            )
            self._wait_until(
                lambda: published == ["vt-child"],
                timeout=2.0,
                interval=0.01,
                message="both committed parent results did not unlock their child",
            )
            coordinator.enqueue_completion_committed(
                completion_service.commit(completions, notify=False)
            )
            coordinator.enqueue_cancel("test shutdown")
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                coordinator.enqueue_cancel("test shutdown")
                thread.join(timeout=2.0)
        rows_by_id = {
            str(row["id"]): row
            for row in store.list_rows(verification_id)
        }
        self.assertEqual(
            str(rows_by_id["vt-parent-a"]["status"]),
            VerificationTaskStatus.DONE,
        )
        self.assertEqual(
            str(rows_by_id["vt-parent-b"]["status"]),
            VerificationTaskStatus.DONE,
        )
        self.assertEqual(published, ["vt-child"])


class TestVerificationRuntimeRegistry(unittest.TestCase):
    @staticmethod
    def _commit(verification_id: str) -> CompletionCommit:
        return CompletionCommit(
            verification_id=verification_id,
            effective_completions=(),
            committed_task_ids=frozenset(),
            already_terminal_task_ids=frozenset(),
            skipped_task_ids=frozenset(),
        )

    def test_registration_is_insert_only_and_unregistration_matches_identity(
        self,
    ) -> None:
        registry = VerificationRuntimeRegistry()
        first = _RegistryHandle()
        second = _RegistryHandle()
        registry.register("ver-runtime", first)

        with self.assertRaisesRegex(RuntimeError, "already registered"):
            registry.register("ver-runtime", second)
        self.assertFalse(registry.unregister("ver-runtime", second))
        self.assertTrue(registry.case_leased("ver-runtime", "task-first"))
        self.assertEqual(first.events, [("leased", "task-first")])

        self.assertTrue(registry.unregister("ver-runtime", first))
        registry.register("ver-runtime", second)
        self.assertFalse(registry.unregister("ver-runtime", first))
        self.assertTrue(registry.cancelled("ver-runtime", "stop"))
        self.assertEqual(second.events, [("cancel", "stop")])

    def test_notifications_preserve_order_and_missing_runtime_is_noop(self) -> None:
        registry = VerificationRuntimeRegistry()
        handle = _RegistryHandle()
        commit = self._commit("ver-events")
        self.assertFalse(registry.case_leased("missing", "task"))
        self.assertFalse(registry.completion_committed("missing", commit))
        self.assertFalse(registry.cancelled("missing", "stop"))

        registry.register("ver-events", handle)
        self.assertTrue(registry.case_leased("ver-events", "task"))
        self.assertTrue(registry.completion_committed("ver-events", commit))
        self.assertTrue(registry.cancelled("ver-events", "stop"))
        self.assertTrue(registry.closed("ver-events"))
        self.assertEqual(
            handle.events,
            [
                ("leased", "task"),
                ("completion", commit),
                ("cancel", "stop"),
                ("closed", ""),
            ],
        )

    def test_completion_notification_falls_back_to_durable_reconciliation(
        self,
    ) -> None:
        registry = VerificationRuntimeRegistry()
        commit = self._commit("ver-reconcile")

        class FailingCompletionHandle(_RegistryHandle):
            def enqueue_completion_committed(self, candidate: CompletionCommit) -> None:
                del candidate
                raise RuntimeError("completion event unavailable")

        handle = FailingCompletionHandle()
        registry.register("ver-reconcile", handle)

        self.assertTrue(registry.completion_committed("ver-reconcile", commit))
        self.assertEqual(handle.events, [("reconcile", commit)])

    def test_case_lease_notification_retries_current_runtime_once(self) -> None:
        registry = VerificationRuntimeRegistry()

        class RetryLeaseHandle(_RegistryHandle):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def enqueue_case_leased(self, verification_task_id: str) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("lease event unavailable")
                super().enqueue_case_leased(verification_task_id)

        handle = RetryLeaseHandle()
        registry.register("ver-lease-retry", handle)

        self.assertTrue(registry.case_leased("ver-lease-retry", "vt-leased"))
        self.assertEqual(handle.attempts, 2)
        self.assertEqual(handle.events, [("leased", "vt-leased")])

        class FailedLeaseHandle(_RegistryHandle):
            def enqueue_case_leased(self, verification_task_id: str) -> None:
                del verification_task_id
                raise RuntimeError("lease event unavailable")

        failed = FailedLeaseHandle()
        registry.register("ver-lease-failure", failed)
        with self.assertRaisesRegex(
            RuntimeError,
            "lease event unavailable; lease event unavailable",
        ) as raised:
            registry.case_leased("ver-lease-failure", "vt-leased")
        self.assertEqual(str(raised.exception.__cause__), "lease event unavailable")

    def test_notification_does_not_hold_registry_lock(self) -> None:
        registry = VerificationRuntimeRegistry()
        callback_entered = threading.Event()
        release_callback = threading.Event()

        class BlockingHandle(_RegistryHandle):
            def enqueue_case_leased(self, verification_task_id: str) -> None:
                callback_entered.set()
                if not release_callback.wait(timeout=2.0):
                    raise RuntimeError("test callback release timed out")
                super().enqueue_case_leased(verification_task_id)

        handle = BlockingHandle()
        registry.register("ver-lock", handle)
        notifier = threading.Thread(
            target=lambda: registry.case_leased("ver-lock", "task"),
            daemon=True,
        )
        notifier.start()
        self.assertTrue(callback_entered.wait(timeout=2.0))
        self.assertTrue(registry.unregister("ver-lock", handle))
        replacement = _RegistryHandle()
        registry.register("ver-lock", replacement)
        release_callback.set()
        notifier.join(timeout=2.0)
        self.assertFalse(notifier.is_alive())
        self.assertEqual(handle.events, [("leased", "task")])
        self.assertTrue(registry.cancelled("ver-lock", "replacement"))
        self.assertEqual(replacement.events, [("cancel", "replacement")])
