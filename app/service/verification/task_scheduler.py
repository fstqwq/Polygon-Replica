from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, cast

from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import is_cancel_reason


@dataclass(frozen=True)
class TaskPublishResult:
    task_id: str
    run_id: str
    judgehost_task_id: str
    terminal_result: TaskExecutionResult | None = None


@dataclass(frozen=True)
class TaskExecutionResult:
    task_id: str
    status: str
    verdict: str
    run_id: str
    judgehost_task_id: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    compile_log: str
    diagnostics_json: str
    error_text: str
    feedback_text: str
    output_ref: str
    fail_flag_reason: str = ""
    answer_correct: bool = False


@dataclass(frozen=True)
class VerificationRuntimeCallbacks:
    publish_task: Callable[[VerificationTaskRow], TaskPublishResult]
    probe_task_case_cache: Callable[[list[str], int], set[str]]
    resolve_case_result: Callable[[str, str], dict[str, object] | None]
    cancel_queued_tasks: Callable[[str], None]
    close_logical_runs: Callable[[list[str]], None]


@dataclass(frozen=True)
class _VerificationEvent:
    kind: str
    judgehost_task_id: str = ""
    test_name: str = ""
    result: dict[str, object] | None = None
    reason: str = ""


_COORDINATOR_LOCK = threading.Lock()
_COORDINATORS_BY_VERIFICATION_ID: dict[str, "VerificationRuntimeCoordinator"] = {}
_CACHE_PROBE_SLICE_SIZE = 32
_RESULT_BATCH_MAX_SIZE = 256
_RESULT_BATCH_MAX_WAIT_SEC = 0.005
_TERMINAL_TASK_STATUSES = frozenset(
    {
        VerificationTaskStore.TASK_DONE,
        VerificationTaskStore.TASK_FAILED,
        VerificationTaskStore.TASK_CANCELLED,
    }
)


class _IncrementalDagState:
    def __init__(self, rows: list[VerificationTaskRow], edges: list[tuple[str, str]]) -> None:
        ordered_rows = sorted(rows, key=lambda row: (int(row["queue_index"]), str(row["id"])))
        self.plan_index_by_id = {
            str(row["id"]): index
            for index, row in enumerate(ordered_rows)
        }
        self.rows_by_id = {
            str(row["id"]): cast(VerificationTaskRow, dict(row))
            for row in ordered_rows
        }
        self.status_by_id = {
            task_id: str(row["status"])
            for task_id, row in self.rows_by_id.items()
        }
        self.logical_run_id_by_task_id: dict[str, str] = {}
        self.remaining_tasks_by_logical_run: dict[str, int] = {}
        for task_id, row in self.rows_by_id.items():
            logical_run_id = str(row["logical_run_id"])
            if not logical_run_id:
                raise RuntimeError(f"verification task {task_id} is missing logical run identity")
            self.logical_run_id_by_task_id[task_id] = logical_run_id
            if self.status_by_id[task_id] not in _TERMINAL_TASK_STATUSES:
                self.remaining_tasks_by_logical_run[logical_run_id] = (
                    self.remaining_tasks_by_logical_run.get(logical_run_id, 0) + 1
                )
        self.completed_logical_run_ids: deque[str] = deque()
        self.dependents_by_parent: dict[str, list[str]] = {}
        self.remaining_parents = {task_id: 0 for task_id in self.rows_by_id}
        for parent_id, child_id in edges:
            if child_id not in self.rows_by_id:
                continue
            if self.status_by_id.get(parent_id) != VerificationTaskStore.TASK_DONE:
                self.remaining_parents[child_id] += 1
            if parent_id in self.rows_by_id:
                self.dependents_by_parent.setdefault(parent_id, []).append(child_id)
        for child_ids in self.dependents_by_parent.values():
            child_ids.sort(key=self.plan_index_by_id.__getitem__)

        self.ready: deque[str] = deque()
        self.ready_ids: set[str] = set()
        for task_id, row in self.rows_by_id.items():
            if self.status_by_id[task_id] == VerificationTaskStore.TASK_PENDING:
                self._enqueue_if_ready(task_id)

        self.task_ids_by_judgehost_id: dict[str, list[str]] = {}
        self.task_id_by_case: dict[tuple[str, str], str] = {}
        for task_id in self.rows_by_id:
            self._index_runtime_identity(task_id)
        self.terminal_count = sum(
            status in _TERMINAL_TASK_STATUSES
            for status in self.status_by_id.values()
        )

    def _enqueue_if_ready(self, task_id: str) -> None:
        if self.remaining_parents[task_id] != 0 or task_id in self.ready_ids:
            return
        self.ready.append(task_id)
        self.ready_ids.add(task_id)

    def pop_ready(self) -> VerificationTaskRow | None:
        while self.ready:
            task_id = self.ready.popleft()
            self.ready_ids.discard(task_id)
            if self.status_by_id[task_id] != VerificationTaskStore.TASK_PENDING:
                continue
            if self.remaining_parents[task_id] != 0:
                continue
            return self.rows_by_id[task_id]
        return None

    def _index_runtime_identity(self, task_id: str) -> None:
        row = self.rows_by_id[task_id]
        judgehost_task_id = str(row["judgehost_task_id"])
        if not judgehost_task_id:
            return
        task_ids = self.task_ids_by_judgehost_id.setdefault(judgehost_task_id, [])
        if task_id not in task_ids:
            task_ids.append(task_id)
        test_name = str(row["test_name"])
        if test_name:
            self.task_id_by_case[(judgehost_task_id, test_name)] = task_id

    def set_runtime_identity(self, task_id: str, *, run_id: str, judgehost_task_id: str) -> None:
        row = self.rows_by_id[task_id]
        row["run_id"] = run_id
        row["judgehost_task_id"] = judgehost_task_id
        self._index_runtime_identity(task_id)

    def transition(self, task_id: str, status: str) -> bool:
        previous = self.status_by_id[task_id]
        if previous == status or previous in _TERMINAL_TASK_STATUSES:
            return False
        self.status_by_id[task_id] = status
        self.rows_by_id[task_id]["status"] = status
        if status not in _TERMINAL_TASK_STATUSES:
            return True
        self.terminal_count += 1
        logical_run_id = self.logical_run_id_by_task_id[task_id]
        remaining = self.remaining_tasks_by_logical_run[logical_run_id] - 1
        if remaining < 0:
            raise RuntimeError("verification logical run task count underflow")
        if remaining == 0:
            self.remaining_tasks_by_logical_run.pop(logical_run_id, None)
            self.completed_logical_run_ids.append(logical_run_id)
        else:
            self.remaining_tasks_by_logical_run[logical_run_id] = remaining
        if status != VerificationTaskStore.TASK_DONE:
            return True
        for child_id in self.dependents_by_parent.get(task_id, []):
            remaining = self.remaining_parents[child_id]
            if remaining <= 0:
                continue
            self.remaining_parents[child_id] = remaining - 1
            if self.status_by_id[child_id] == VerificationTaskStore.TASK_PENDING:
                self._enqueue_if_ready(child_id)
        return True

    def take_completed_logical_runs(self) -> list[str]:
        values = list(self.completed_logical_run_ids)
        self.completed_logical_run_ids.clear()
        return values

    def task_for_case(self, judgehost_task_id: str, test_name: str) -> VerificationTaskRow | None:
        task_id = self.task_id_by_case.get((judgehost_task_id, test_name))
        if task_id is None:
            return None
        return self.rows_by_id[task_id]

    def tasks_for_judgehost_id(self, judgehost_task_id: str) -> list[VerificationTaskRow]:
        return [
            self.rows_by_id[task_id]
            for task_id in self.task_ids_by_judgehost_id.get(judgehost_task_id, [])
        ]

    def is_terminal(self) -> bool:
        return bool(self.rows_by_id) and self.terminal_count == len(self.rows_by_id)


def _save_result(
    *,
    verification_id: str,
    task_store: VerificationTaskStore,
    result: TaskExecutionResult,
) -> None:
    _save_results(verification_id=verification_id, task_store=task_store, results=[result])


def _save_results(
    *,
    verification_id: str,
    task_store: VerificationTaskStore,
    results: list[TaskExecutionResult],
) -> None:
    task_store.save_task_results(
        [
            {
                "task_id": result.task_id,
                "status": result.status,
                "verdict": result.verdict,
                "run_id": result.run_id,
                "judgehost_task_id": result.judgehost_task_id,
                "runtime_sec": result.runtime_sec,
                "cpu_sec": result.cpu_sec,
                "wall_sec": result.wall_sec,
                "memory_kb": result.memory_kb,
                "compile_log": result.compile_log,
                "diagnostics_json": result.diagnostics_json,
                "error_text": result.error_text,
                "feedback_text": result.feedback_text,
                "output_ref": result.output_ref,
                "answer_correct": result.answer_correct,
            }
            for result in results
        ]
    )
    for result in results:
        if result.fail_flag_reason:
            task_store.set_fail_flag(verification_id, reason=result.fail_flag_reason)


class VerificationRuntimeCoordinator:
    def __init__(
        self,
        verification_id: str,
        *,
        task_store: VerificationTaskStore,
        callbacks: VerificationRuntimeCallbacks,
        edges: list[tuple[str, str]],
    ) -> None:
        self.verification_id = verification_id
        self._task_store = task_store
        self._callbacks = callbacks
        self._dag = _IncrementalDagState(task_store.list_rows(verification_id), edges)
        self._events: queue.Queue[_VerificationEvent] = queue.Queue()
        self._cache_probe_task_ids: dict[str, None] = {}
        self._applied_fail_reason = ""
        self._cancel_reason = ""

    def enqueue_bootstrap(self) -> None:
        self._events.put(_VerificationEvent(kind="bootstrap"))

    def enqueue_case_leased(self, judgehost_task_id: str, test_name: str) -> None:
        self._events.put(
            _VerificationEvent(
                kind="case_leased",
                judgehost_task_id=judgehost_task_id,
                test_name=test_name,
            )
        )

    def enqueue_case_reported(
        self,
        judgehost_task_id: str,
        test_name: str,
        result: dict[str, object],
    ) -> None:
        self._events.put(
            _VerificationEvent(
                kind="case_reported",
                judgehost_task_id=judgehost_task_id,
                test_name=test_name,
                result=result,
            )
        )

    def enqueue_task_terminal(self, judgehost_task_id: str) -> None:
        self._events.put(
            _VerificationEvent(
                kind="task_terminal",
                judgehost_task_id=judgehost_task_id,
            )
        )

    def enqueue_cancel(self, reason: str) -> None:
        self._events.put(_VerificationEvent(kind="cancel", reason=reason))

    def run(self) -> None:
        self.enqueue_bootstrap()
        deferred: _VerificationEvent | None = None
        while True:
            event = self._events.get() if deferred is None else deferred
            deferred = None
            if event.kind not in {"case_reported", "task_terminal"}:
                terminal = self._handle_event(event)
                self._close_completed_logical_runs()
                if terminal:
                    return
                continue
            events = [event]
            deadline = time.monotonic() + _RESULT_BATCH_MAX_WAIT_SEC
            while len(events) < _RESULT_BATCH_MAX_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    candidate = self._events.get(timeout=remaining)
                except queue.Empty:
                    break
                if candidate.kind not in {"case_reported", "task_terminal"}:
                    deferred = candidate
                    break
                events.append(candidate)
            terminal = self._handle_terminal_events(events)
            self._close_completed_logical_runs()
            if terminal:
                return

    def _close_completed_logical_runs(self) -> None:
        logical_run_ids = self._dag.take_completed_logical_runs()
        if logical_run_ids:
            self._callbacks.close_logical_runs(logical_run_ids)

    def _handle_terminal_events(self, events: list[_VerificationEvent]) -> bool:
        from app.service.verification.task_result_finalize import finalize_verification_task_result

        prepared: list[tuple[str, TaskExecutionResult]] = []
        prepared_task_ids: set[str] = set()
        for event in events:
            if event.kind == "case_reported":
                if event.result is None:
                    continue
                row = self._dag.task_for_case(event.judgehost_task_id, event.test_name)
                if row is None:
                    continue
                task_id = str(row["id"])
                if (
                    task_id in prepared_task_ids
                    or self._dag.status_by_id[task_id] in _TERMINAL_TASK_STATUSES
                ):
                    continue
                if "final_result" in event.result:
                    result = cast(TaskExecutionResult, event.result["final_result"])
                else:
                    result = finalize_verification_task_result(row, result=event.result)
                prepared.append((task_id, result))
                prepared_task_ids.add(task_id)
                continue
            if not event.judgehost_task_id:
                continue
            for row in self._dag.tasks_for_judgehost_id(event.judgehost_task_id):
                task_id = str(row["id"])
                if task_id in prepared_task_ids:
                    continue
                if self._dag.status_by_id[task_id] not in {
                    VerificationTaskStore.TASK_QUEUED,
                    VerificationTaskStore.TASK_LEASED,
                }:
                    continue
                result = self._callbacks.resolve_case_result(
                    event.judgehost_task_id,
                    str(row["test_name"]),
                )
                if result is None:
                    continue
                prepared.append((task_id, finalize_verification_task_result(row, result=result)))
                prepared_task_ids.add(task_id)
        if not prepared:
            return False
        _save_results(
            verification_id=self.verification_id,
            task_store=self._task_store,
            results=[result for _task_id, result in prepared],
        )
        for task_id, result in prepared:
            self._dag.transition(task_id, result.status)
        self._publish_ready_rows()
        self._apply_fail_flag()
        if self._is_terminal():
            return True
        return False

    def _handle_event(self, event: _VerificationEvent) -> bool:
        if event.kind == "bootstrap":
            self._publish_ready_rows()
        elif event.kind == "case_leased":
            self._mark_case_leased(event.judgehost_task_id, event.test_name)
        elif event.kind == "case_reported":
            changed = self._finalize_case_result(
                event.judgehost_task_id,
                event.test_name,
                event.result,
            )
            if changed:
                changed = self._publish_ready_rows() or changed
        elif event.kind == "task_terminal":
            changed = self._finalize_terminal_task(event.judgehost_task_id)
            if changed:
                changed = self._publish_ready_rows() or changed
        elif event.kind == "cancel":
            self._cancel_reason = event.reason or "verification cancelled by user"
            self._task_store.set_fail_flag(self.verification_id, reason=self._cancel_reason)
            return True
        self._apply_fail_flag()
        if self._is_terminal():
            return True
        return False

    def _is_terminal(self) -> bool:
        return self._dag.is_terminal()

    def _apply_fail_flag(self) -> bool:
        fail_flag, fail_reason = self._task_store.fail_state(self.verification_id)
        if not fail_flag:
            self._applied_fail_reason = ""
            self._cancel_reason = ""
            return False
        cancel_reason = fail_reason or "cancelled after main-correct failure"
        if cancel_reason == self._applied_fail_reason:
            return False
        if (
            self._cancel_reason
            and cancel_reason == self._cancel_reason
            and is_cancel_reason(cancel_reason)
        ):
            self._applied_fail_reason = cancel_reason
            return False
        self._callbacks.cancel_queued_tasks(cancel_reason)
        cancelled_task_ids = [
            task_id
            for task_id, status in self._dag.status_by_id.items()
            if status in {VerificationTaskStore.TASK_PENDING, VerificationTaskStore.TASK_QUEUED}
        ]
        self._task_store.cancel_not_started_tasks(self.verification_id, reason=cancel_reason)
        for task_id in cancelled_task_ids:
            self._dag.transition(task_id, VerificationTaskStore.TASK_CANCELLED)
        self._applied_fail_reason = cancel_reason
        return True

    def _publish_ready_rows(self) -> bool:
        if self._task_store.fail_state(self.verification_id)[0]:
            return False
        changed = False
        published_count = 0
        while (
            published_count < _CACHE_PROBE_SLICE_SIZE
            and len(self._cache_probe_task_ids) < _CACHE_PROBE_SLICE_SIZE
        ):
            row = self._dag.pop_ready()
            if row is None:
                break
            published = self._callbacks.publish_task(row)
            task_id = str(row["id"])
            if published.terminal_result is None:
                self._task_store.set_task_queued(
                    task_id,
                    run_id=published.run_id,
                    judgehost_task_id=published.judgehost_task_id,
                )
                self._dag.set_runtime_identity(
                    task_id,
                    run_id=published.run_id,
                    judgehost_task_id=published.judgehost_task_id,
                )
                self._dag.transition(task_id, VerificationTaskStore.TASK_QUEUED)
                self._cache_probe_task_ids[published.judgehost_task_id] = None
            else:
                _save_result(
                    verification_id=self.verification_id,
                    task_store=self._task_store,
                    result=published.terminal_result,
                )
                self._dag.set_runtime_identity(
                    task_id,
                    run_id=published.terminal_result.run_id,
                    judgehost_task_id=published.terminal_result.judgehost_task_id,
                )
                self._dag.transition(task_id, published.terminal_result.status)
            published_count += 1
            changed = True
            if not self._events.empty():
                return changed
        if self._cache_probe_task_ids:
            selected = list(self._cache_probe_task_ids)[:_CACHE_PROBE_SLICE_SIZE]
            pending = self._callbacks.probe_task_case_cache(selected, _CACHE_PROBE_SLICE_SIZE)
            for judgehost_task_id in selected:
                if judgehost_task_id not in pending:
                    self._cache_probe_task_ids.pop(judgehost_task_id, None)
            if not self._events.empty():
                return changed
            if pending:
                # Yield to queued result events between slices instead of publishing an
                # unbounded ready graph in one coordinator turn.
                self.enqueue_bootstrap()
        if published_count == _CACHE_PROBE_SLICE_SIZE and self._events.empty():
            self.enqueue_bootstrap()
        return changed

    def _mark_case_leased(self, judgehost_task_id: str, test_name: str) -> bool:
        row = self._dag.task_for_case(judgehost_task_id, test_name)
        if row is None:
            return False
        if str(row["status"]) != VerificationTaskStore.TASK_QUEUED:
            return False
        self._task_store.set_task_leased(str(row["id"]))
        self._dag.transition(str(row["id"]), VerificationTaskStore.TASK_LEASED)
        return True

    def _finalize_case_result(
        self,
        judgehost_task_id: str,
        test_name: str,
        result: dict[str, object] | None,
    ) -> bool:
        if result is None:
            return False
        row = self._dag.task_for_case(judgehost_task_id, test_name)
        if row is None:
            return False
        task_id = str(row["id"])
        if self._dag.status_by_id[task_id] in _TERMINAL_TASK_STATUSES:
            return False
        final_result = result
        execution_result = cast(
            TaskExecutionResult,
            final_result["final_result"] if "final_result" in final_result else final_result,
        )
        _save_result(
            verification_id=self.verification_id,
            task_store=self._task_store,
            result=execution_result,
        )
        self._dag.transition(task_id, execution_result.status)
        return True

    def _finalize_terminal_task(self, judgehost_task_id: str) -> bool:
        if not judgehost_task_id:
            return False
        changed = False
        for row in self._dag.tasks_for_judgehost_id(judgehost_task_id):
            status = str(row["status"])
            if status not in {VerificationTaskStore.TASK_QUEUED, VerificationTaskStore.TASK_LEASED}:
                continue
            result = self._callbacks.resolve_case_result(judgehost_task_id, str(row["test_name"]))
            if result is None:
                continue
            from app.service.verification.task_result_finalize import finalize_verification_task_result

            final_result = finalize_verification_task_result(row, result=result)
            _save_result(
                verification_id=self.verification_id,
                task_store=self._task_store,
                result=final_result,
            )
            self._dag.transition(str(row["id"]), final_result.status)
            changed = True
        return changed


def register_verification_runtime_coordinator(
    verification_id: str,
    coordinator: VerificationRuntimeCoordinator,
) -> None:
    with _COORDINATOR_LOCK:
        _COORDINATORS_BY_VERIFICATION_ID[verification_id] = coordinator


def unregister_verification_runtime_coordinator(verification_id: str) -> None:
    with _COORDINATOR_LOCK:
        _COORDINATORS_BY_VERIFICATION_ID.pop(verification_id, None)


def _runtime_coordinator(verification_id: str) -> VerificationRuntimeCoordinator | None:
    with _COORDINATOR_LOCK:
        return _COORDINATORS_BY_VERIFICATION_ID.get(verification_id)


def notify_verification_case_leased(
    verification_id: str,
    judgehost_task_id: str,
    test_name: str,
) -> None:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return
    coordinator.enqueue_case_leased(judgehost_task_id, test_name)


def notify_verification_case_reported(
    verification_id: str,
    judgehost_task_id: str,
    test_name: str,
    result: dict[str, object],
) -> bool:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return False
    coordinator.enqueue_case_reported(
        judgehost_task_id,
        test_name,
        result,
    )
    return True


def notify_verification_task_terminal(
    verification_id: str,
    judgehost_task_id: str,
) -> bool:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return False
    coordinator.enqueue_task_terminal(judgehost_task_id)
    return True


def notify_verification_cancelled(verification_id: str, reason: str) -> None:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return
    coordinator.enqueue_cancel(reason)
