from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, cast

from app.service.judgehost.case_result import CaseTerminalReport
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.task_completion import CompletionCommit, TaskCompletion
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore


@dataclass(frozen=True)
class TaskPublishResult:
    task_id: str
    run_id: str
    judgehost_task_id: str
    terminal_result: TaskCompletion | None = None


@dataclass(frozen=True)
class VerificationRuntimeCallbacks:
    publish_task: Callable[[VerificationTaskRow], TaskPublishResult]
    probe_task_case_cache: Callable[[list[str]], set[str]]
    resolve_case_result: Callable[[str, str], CaseTerminalReport | None]
    cancel_execution: Callable[[str], None]
    close_logical_runs: Callable[[list[str]], None]
    reconcile_expired_leases: Callable[[], list[str]] = lambda: []


@dataclass(frozen=True)
class _VerificationEvent:
    kind: str
    judgehost_task_id: str = ""
    test_name: str = ""
    completion_commit: CompletionCommit | None = None
    reason: str = ""


_COORDINATOR_LOCK = threading.Lock()
_COORDINATORS_BY_VERIFICATION_ID: dict[str, "VerificationRuntimeCoordinator"] = {}
COORDINATOR_BATCH_SIZE = 256
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

    def mark_skipped(self, task_id: str, *, feedback_text: str) -> bool:
        previous = self.status_by_id[task_id]
        if previous in _TERMINAL_TASK_STATUSES:
            return False
        self.status_by_id[task_id] = VerificationTaskStore.TASK_DONE
        row = self.rows_by_id[task_id]
        row["status"] = VerificationTaskStore.TASK_DONE
        row["verdict"] = "SK"
        row["feedback_text"] = feedback_text
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
        return True

    def set_result_metadata(self, task_id: str, *, verdict: str, feedback_text: str) -> None:
        row = self.rows_by_id[task_id]
        row["verdict"] = verdict
        row["feedback_text"] = feedback_text

    def requeue(self, task_id: str) -> bool:
        if self.status_by_id[task_id] != VerificationTaskStore.TASK_LEASED:
            return False
        self.status_by_id[task_id] = VerificationTaskStore.TASK_QUEUED
        self.rows_by_id[task_id]["status"] = VerificationTaskStore.TASK_QUEUED
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


class VerificationRuntimeCoordinator:
    def __init__(
        self,
        verification_id: str,
        *,
        task_store: VerificationTaskStore,
        completion_service: VerificationTaskCompletionService,
        callbacks: VerificationRuntimeCallbacks,
        edges: list[tuple[str, str]],
    ) -> None:
        self.verification_id = verification_id
        self._task_store = task_store
        self._completion_service = completion_service
        self._callbacks = callbacks
        self._dag = _IncrementalDagState(task_store.list_rows(verification_id), edges)
        self._events: queue.Queue[_VerificationEvent] = queue.Queue()
        self._cache_probe_task_ids: dict[str, None] = {}
        self._applied_fail_reason = ""

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

    def enqueue_completion_committed(self, commit: CompletionCommit) -> None:
        if commit.verification_id != self.verification_id:
            raise RuntimeError("completion commit verification does not match coordinator")
        self._events.put(
            _VerificationEvent(
                kind="completion_committed",
                completion_commit=commit,
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
            if deferred is None:
                try:
                    event = self._events.get(timeout=10.0)
                except queue.Empty:
                    self._reconcile_expired_leases()
                    self._close_completed_logical_runs()
                    if self._is_terminal():
                        return
                    continue
            else:
                event = deferred
            deferred = None
            if event.kind not in {"completion_committed", "task_terminal"}:
                terminal = self._handle_event(event)
                if not terminal:
                    self._reconcile_expired_leases()
                self._close_completed_logical_runs()
                if terminal:
                    return
                if self._is_terminal():
                    return
                continue
            events = [event]
            deadline = time.monotonic() + _RESULT_BATCH_MAX_WAIT_SEC
            while len(events) < COORDINATOR_BATCH_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    candidate = self._events.get(timeout=remaining)
                except queue.Empty:
                    break
                if candidate.kind not in {"completion_committed", "task_terminal"}:
                    deferred = candidate
                    break
                events.append(candidate)
            terminal = self._handle_terminal_events(events)
            if not terminal:
                self._reconcile_expired_leases()
            self._close_completed_logical_runs()
            if terminal:
                return
            if self._is_terminal():
                return

    def _reconcile_expired_leases(self) -> bool:
        judgehost_task_ids = self._callbacks.reconcile_expired_leases()
        if not judgehost_task_ids:
            return False
        task_ids = self._task_store.requeue_leased_tasks(
            self.verification_id,
            judgehost_task_ids,
        )
        changed = False
        for task_id in task_ids:
            changed = self._dag.requeue(task_id) or changed
        return changed

    def _close_completed_logical_runs(self) -> None:
        logical_run_ids = self._dag.take_completed_logical_runs()
        if logical_run_ids:
            self._callbacks.close_logical_runs(logical_run_ids)

    def _apply_completion_commit(self, commit: CompletionCommit) -> bool:
        if commit.verification_id != self.verification_id:
            raise RuntimeError("completion commit verification does not match coordinator")
        skipped_task_ids = set(commit.skipped_task_ids)
        changed = False
        for result in commit.effective_completions:
            task_id = result.task_id
            if task_id not in self._dag.rows_by_id:
                continue
            if task_id in skipped_task_ids:
                feedback_text = (
                    result.feedback_text
                    if result.verdict.upper() == "SK" and result.feedback_text
                    else "skipped because generate-input was skipped"
                )
                self._dag.set_result_metadata(
                    task_id,
                    verdict="SK",
                    feedback_text=feedback_text,
                )
                changed = (
                    self._dag.mark_skipped(task_id, feedback_text=feedback_text)
                    or changed
                )
                continue
            self._dag.set_result_metadata(
                task_id,
                verdict=result.verdict,
                feedback_text=result.feedback_text,
            )
            changed = self._dag.transition(task_id, result.status) or changed
        for task_id in skipped_task_ids:
            if task_id not in self._dag.rows_by_id:
                continue
            changed = (
                self._dag.mark_skipped(
                    task_id,
                    feedback_text="skipped because generate-input was skipped",
                )
                or changed
            )
        return changed

    def _handle_terminal_events(self, events: list[_VerificationEvent]) -> bool:
        changed = False
        prepared: list[TaskCompletion] = []
        prepared_task_ids: set[str] = set()
        for event in events:
            if event.kind == "completion_committed":
                if event.completion_commit is not None:
                    changed = (
                        self._apply_completion_commit(event.completion_commit)
                        or changed
                    )
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
                prepared.append(self._completion_service.prepare(row, result))
                prepared_task_ids.add(task_id)
        if prepared:
            commit = self._completion_service.commit(prepared, notify=False)
            changed = self._apply_completion_commit(commit) or changed
        if changed:
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
        elif event.kind == "cancel":
            reason = event.reason or "verification cancelled by user"
            self._task_store.set_fail_flag(self.verification_id, reason=reason)
            self._apply_fail_flag()
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
            return False
        cancel_reason = fail_reason or "cancelled after main-correct failure"
        if cancel_reason == self._applied_fail_reason:
            return False
        self._callbacks.cancel_execution(cancel_reason)
        rows_by_id = {
            str(row["id"]): row
            for row in self._task_store.list_rows(self.verification_id)
        }
        for task_id, row in rows_by_id.items():
            if str(row["status"]) == VerificationTaskStore.TASK_CANCELLED:
                self._dag.transition(task_id, VerificationTaskStore.TASK_CANCELLED)
        self._applied_fail_reason = cancel_reason
        return True

    def _publish_ready_rows(self) -> bool:
        if self._task_store.fail_state(self.verification_id)[0]:
            return False
        changed = False
        published_count = 0
        while (
            published_count < COORDINATOR_BATCH_SIZE
            and len(self._cache_probe_task_ids) < COORDINATOR_BATCH_SIZE
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
                commit = self._completion_service.commit(
                    [published.terminal_result],
                    notify=False,
                )
                self._dag.set_runtime_identity(
                    task_id,
                    run_id=published.terminal_result.run_id,
                    judgehost_task_id=published.terminal_result.judgehost_task_id,
                )
                self._apply_completion_commit(commit)
            published_count += 1
            changed = True
            if not self._events.empty():
                return changed
        if self._cache_probe_task_ids:
            selected = list(self._cache_probe_task_ids)[:COORDINATOR_BATCH_SIZE]
            pending = self._callbacks.probe_task_case_cache(selected)
            for judgehost_task_id in selected:
                if judgehost_task_id not in pending:
                    self._cache_probe_task_ids.pop(judgehost_task_id, None)
            if not self._events.empty():
                return changed
            if pending:
                # Yield to queued result events between slices instead of publishing an
                # unbounded ready graph in one coordinator turn.
                self.enqueue_bootstrap()
        if published_count == COORDINATOR_BATCH_SIZE and self._events.empty():
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


def notify_verification_completion_committed(
    verification_id: str,
    commit: CompletionCommit,
) -> bool:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return False
    coordinator.enqueue_completion_committed(commit)
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
