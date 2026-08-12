from __future__ import annotations

import queue
from collections import deque
from dataclasses import dataclass
from typing import Callable, cast

from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.task_completion import CompletionCommit, TaskCompletion
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import VerificationTaskStatus


_PUBLISH_SLICE_SIZE = 256
_IDLE_RECONCILIATION_SEC = 10.0


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
    cancel_execution: Callable[[str], None]
    close_programs: Callable[[list[str]], None]
    reconcile_expired_leases: Callable[[], list[str]] = lambda: []


@dataclass(frozen=True)
class _VerificationEvent:
    kind: str
    verification_task_id: str = ""
    completion_commit: CompletionCommit | None = None
    reason: str = ""


_TERMINAL_TASK_STATUSES = frozenset(
    {
        VerificationTaskStatus.DONE,
        VerificationTaskStatus.FAILED,
        VerificationTaskStatus.CANCELLED,
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
            task_id: row["status"]
            for task_id, row in self.rows_by_id.items()
        }
        self.program_id_by_task_id: dict[str, str] = {}
        self.remaining_tasks_by_program: dict[str, int] = {}
        for task_id, row in self.rows_by_id.items():
            program_id = str(row["program_id"])
            if not program_id:
                raise RuntimeError(f"verification task {task_id} is missing program identity")
            self.program_id_by_task_id[task_id] = program_id
            if self.status_by_id[task_id] not in _TERMINAL_TASK_STATUSES:
                self.remaining_tasks_by_program[program_id] = (
                    self.remaining_tasks_by_program.get(program_id, 0) + 1
                )
        self.completed_program_ids: deque[str] = deque()
        self.dependents_by_parent: dict[str, list[str]] = {}
        self.remaining_parents = {task_id: 0 for task_id in self.rows_by_id}
        for parent_id, child_id in edges:
            if child_id not in self.rows_by_id:
                continue
            if self.status_by_id.get(parent_id) != VerificationTaskStatus.DONE:
                self.remaining_parents[child_id] += 1
            if parent_id in self.rows_by_id:
                self.dependents_by_parent.setdefault(parent_id, []).append(child_id)
        for child_ids in self.dependents_by_parent.values():
            child_ids.sort(key=self.plan_index_by_id.__getitem__)

        self.ready: deque[str] = deque()
        self.ready_ids: set[str] = set()
        for task_id, row in self.rows_by_id.items():
            if self.status_by_id[task_id] == VerificationTaskStatus.PENDING:
                self._enqueue_if_ready(task_id)

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
            if self.status_by_id[task_id] != VerificationTaskStatus.PENDING:
                continue
            if self.remaining_parents[task_id] != 0:
                continue
            return self.rows_by_id[task_id]
        return None

    def set_runtime_identity(self, task_id: str, *, run_id: str, judgehost_task_id: str) -> None:
        row = self.rows_by_id[task_id]
        row["run_id"] = run_id
        row["judgehost_task_id"] = judgehost_task_id

    def transition(
        self,
        task_id: str,
        status: VerificationTaskStatus,
    ) -> bool:
        previous = self.status_by_id[task_id]
        if previous == status or previous in _TERMINAL_TASK_STATUSES:
            return False
        self.status_by_id[task_id] = status
        self.rows_by_id[task_id]["status"] = status
        if status not in _TERMINAL_TASK_STATUSES:
            return True
        self.terminal_count += 1
        program_id = self.program_id_by_task_id[task_id]
        remaining = self.remaining_tasks_by_program[program_id] - 1
        if remaining < 0:
            raise RuntimeError("verification program task count underflow")
        if remaining == 0:
            self.remaining_tasks_by_program.pop(program_id, None)
            self.completed_program_ids.append(program_id)
        else:
            self.remaining_tasks_by_program[program_id] = remaining
        if status != VerificationTaskStatus.DONE:
            return True
        for child_id in self.dependents_by_parent.get(task_id, []):
            remaining = self.remaining_parents[child_id]
            if remaining <= 0:
                continue
            self.remaining_parents[child_id] = remaining - 1
            if self.status_by_id[child_id] == VerificationTaskStatus.PENDING:
                self._enqueue_if_ready(child_id)
        return True

    def mark_skipped(self, task_id: str, *, feedback_text: str) -> bool:
        previous = self.status_by_id[task_id]
        if previous in _TERMINAL_TASK_STATUSES:
            return False
        self.status_by_id[task_id] = VerificationTaskStatus.DONE
        row = self.rows_by_id[task_id]
        row["status"] = VerificationTaskStatus.DONE
        row["verdict"] = "SK"
        row["feedback_text"] = feedback_text
        self.terminal_count += 1
        program_id = self.program_id_by_task_id[task_id]
        remaining = self.remaining_tasks_by_program[program_id] - 1
        if remaining < 0:
            raise RuntimeError("verification program task count underflow")
        if remaining == 0:
            self.remaining_tasks_by_program.pop(program_id, None)
            self.completed_program_ids.append(program_id)
        else:
            self.remaining_tasks_by_program[program_id] = remaining
        return True

    def set_result_metadata(self, task_id: str, *, verdict: str, feedback_text: str) -> None:
        row = self.rows_by_id[task_id]
        row["verdict"] = verdict
        row["feedback_text"] = feedback_text

    def requeue(self, task_id: str) -> bool:
        if self.status_by_id[task_id] != VerificationTaskStatus.LEASED:
            return False
        self.status_by_id[task_id] = VerificationTaskStatus.QUEUED
        self.rows_by_id[task_id]["status"] = VerificationTaskStatus.QUEUED
        return True

    def take_completed_programs(self) -> list[str]:
        values = list(self.completed_program_ids)
        self.completed_program_ids.clear()
        return values

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

    def enqueue_bootstrap(self) -> None:
        self._events.put(_VerificationEvent(kind="bootstrap"))

    def enqueue_case_leased(self, verification_task_id: str) -> None:
        self._events.put(
            _VerificationEvent(
                kind="case_leased",
                verification_task_id=verification_task_id,
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

    def enqueue_completion_reconciliation(self, commit: CompletionCommit) -> None:
        if commit.verification_id != self.verification_id:
            raise RuntimeError("completion commit verification does not match coordinator")
        self._events.put(
            _VerificationEvent(
                kind="completion_reconciliation",
                completion_commit=commit,
            )
        )

    def enqueue_cancel(self, reason: str) -> None:
        self._events.put(_VerificationEvent(kind="cancel", reason=reason))

    def enqueue_closed(self) -> None:
        self._events.put(_VerificationEvent(kind="closed"))

    def run(self) -> None:
        self.enqueue_bootstrap()
        while True:
            try:
                event = self._events.get(timeout=_IDLE_RECONCILIATION_SEC)
            except queue.Empty:
                if not self._task_store.verification_is_running(
                    self.verification_id
                ):
                    self._handle_event(_VerificationEvent(kind="closed"))
                    self._close_completed_programs()
                    self._callbacks.cancel_execution(
                        "verification is no longer running"
                    )
                    return
                changed = self._reconcile_persisted_tasks()
                if changed:
                    self._publish_ready_rows()
                self._reconcile_expired_leases()
                self._close_completed_programs()
                if self._is_terminal():
                    return
                continue
            terminal = self._handle_event(event)
            if not terminal:
                self._reconcile_expired_leases()
            self._close_completed_programs()
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

    def _close_completed_programs(self) -> None:
        program_ids = self._dag.take_completed_programs()
        if program_ids:
            self._callbacks.close_programs(program_ids)

    def _reconcile_persisted_tasks(self) -> bool:
        changed = False
        for row in self._task_store.list_rows(self.verification_id):
            task_id = str(row["id"])
            status = row["status"]
            if (
                task_id not in self._dag.rows_by_id
                or status not in _TERMINAL_TASK_STATUSES
            ):
                continue
            self._dag.set_runtime_identity(
                task_id,
                run_id=str(row["run_id"]),
                judgehost_task_id=str(row["judgehost_task_id"]),
            )
            self._dag.set_result_metadata(
                task_id,
                verdict=str(row["verdict"]),
                feedback_text=str(row["feedback_text"]),
            )
            changed = self._dag.transition(task_id, status) or changed
        return changed

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
        for task_id in commit.cancelled_task_ids:
            if task_id not in self._dag.rows_by_id:
                continue
            changed = (
                self._dag.transition(
                    task_id,
                    VerificationTaskStatus.CANCELLED,
                )
                or changed
            )
        if commit.parent_transition == "failed":
            self._callbacks.cancel_execution(
                commit.failure_reason or "verification failed"
            )
        return changed

    def _handle_event(self, event: _VerificationEvent) -> bool:
        if event.kind == "bootstrap":
            self._publish_ready_rows()
        elif event.kind == "case_leased":
            self._mark_case_leased(event.verification_task_id)
        elif event.kind == "completion_committed":
            commit = event.completion_commit
            changed = False if commit is None else self._apply_completion_commit(commit)
            if changed and self._task_store.verification_is_running(
                self.verification_id
            ):
                self._publish_ready_rows()
        elif event.kind == "completion_reconciliation":
            commit = event.completion_commit
            changed = self._reconcile_persisted_tasks()
            if commit is not None and commit.parent_transition == "failed":
                self._callbacks.cancel_execution(
                    commit.failure_reason or "verification failed"
                )
            if changed and self._task_store.verification_is_running(
                self.verification_id
            ):
                self._publish_ready_rows()
        elif event.kind in {"cancel", "closed"}:
            rows_by_id = {
                str(row["id"]): row
                for row in self._task_store.list_rows(self.verification_id)
            }
            for task_id, row in rows_by_id.items():
                status = row["status"]
                if status in _TERMINAL_TASK_STATUSES:
                    self._dag.set_result_metadata(
                        task_id,
                        verdict=str(row["verdict"]),
                        feedback_text=str(row["feedback_text"]),
                    )
                    self._dag.transition(
                        task_id,
                        status,
                    )
            return True
        if self._is_terminal():
            return True
        return False

    def _is_terminal(self) -> bool:
        return self._dag.is_terminal()

    def _publish_ready_rows(self) -> bool:
        if not self._task_store.verification_is_running(self.verification_id):
            return False
        changed = False
        published_count = 0
        while (
            published_count < _PUBLISH_SLICE_SIZE
            and len(self._cache_probe_task_ids)
            < _PUBLISH_SLICE_SIZE
        ):
            row = self._dag.pop_ready()
            if row is None:
                break
            published = self._callbacks.publish_task(row)
            task_id = str(row["id"])
            if published.terminal_result is None:
                self._dag.set_runtime_identity(
                    task_id,
                    run_id=published.run_id,
                    judgehost_task_id=published.judgehost_task_id,
                )
                self._dag.transition(task_id, VerificationTaskStatus.QUEUED)
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
                if commit.parent_transition == "failed":
                    return changed
            published_count += 1
            changed = True
            if not self._events.empty():
                return changed
        if self._cache_probe_task_ids:
            selected = list(self._cache_probe_task_ids)[
                :_PUBLISH_SLICE_SIZE
            ]
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
        if (
            published_count == _PUBLISH_SLICE_SIZE
            and self._events.empty()
        ):
            self.enqueue_bootstrap()
        return changed

    def _mark_case_leased(self, verification_task_id: str) -> bool:
        row = self._dag.rows_by_id.get(verification_task_id)
        if row is None:
            return False
        if row["status"] != VerificationTaskStatus.QUEUED:
            return False
        if not self._task_store.set_task_leased(verification_task_id):
            return False
        self._dag.transition(
            verification_task_id,
            VerificationTaskStatus.LEASED,
        )
        return True
