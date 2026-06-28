from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from .task_store import VerificationTaskRow, VerificationTaskStore
from .types import is_cancel_reason


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
    resolve_case_result: Callable[[str, str], dict[str, object] | None]
    cancel_queued_tasks: Callable[[str], None]
    persist_state: Callable[[], dict[str, object]]


@dataclass(frozen=True)
class _VerificationEvent:
    kind: str
    judgehost_task_id: str = ""
    test_name: str = ""
    result: dict[str, object] | None = None
    reason: str = ""


_COORDINATOR_LOCK = threading.Lock()
_COORDINATORS_BY_VERIFICATION_ID: dict[str, "VerificationRuntimeCoordinator"] = {}
_PUBLISH_READY_BATCH_SIZE = 256


def _parent_status_by_child(
    rows: list[VerificationTaskRow],
    edges: list[tuple[str, str]],
) -> dict[str, list[str]]:
    status_by_id = {str(row["id"]): str(row["status"]) for row in rows}
    parents_by_child: dict[str, list[str]] = {}
    for parent_id, child_id in edges:
        parents = parents_by_child.get(child_id)
        if parents is None:
            parents = []
            parents_by_child[child_id] = parents
        parents.append(status_by_id.get(parent_id, ""))
    return parents_by_child


def _ready_rows(
    rows: list[VerificationTaskRow],
    edges: list[tuple[str, str]],
) -> list[VerificationTaskRow]:
    parent_statuses = _parent_status_by_child(rows, edges)
    ready: list[VerificationTaskRow] = []
    for row in rows:
        if str(row["status"]) != VerificationTaskStore.TASK_PENDING:
            continue
        task_id = str(row["id"])
        statuses = parent_statuses.get(task_id, [])
        if statuses and any(token != VerificationTaskStore.TASK_DONE for token in statuses):
            continue
        ready.append(row)
    return ready


def _save_result(
    *,
    verification_id: str,
    task_store: VerificationTaskStore,
    result: TaskExecutionResult,
) -> None:
    task_store.save_task_result(
        result.task_id,
        status=result.status,
        verdict=result.verdict,
        run_id=result.run_id,
        judgehost_task_id=result.judgehost_task_id,
        runtime_sec=result.runtime_sec,
        cpu_sec=result.cpu_sec,
        wall_sec=result.wall_sec,
        memory_kb=result.memory_kb,
        compile_log=result.compile_log,
        diagnostics_json=result.diagnostics_json,
        error_text=result.error_text,
        feedback_text=result.feedback_text,
        output_ref=result.output_ref,
        answer_correct=result.answer_correct,
    )
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
        self._edges = list(edges)
        self._events: queue.Queue[_VerificationEvent] = queue.Queue()
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
        while True:
            event = self._events.get()
            if self._handle_event(event):
                return

    def _handle_event(self, event: _VerificationEvent) -> bool:
        changed = False
        if event.kind == "bootstrap":
            changed = self._publish_ready_rows()
        elif event.kind == "case_leased":
            changed = self._mark_case_leased(event.judgehost_task_id, event.test_name)
        elif event.kind == "case_reported":
            changed = self._finalize_case_result(event.judgehost_task_id, event.test_name, event.result)
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
        if self._apply_fail_flag():
            changed = True
        if changed:
            self._callbacks.persist_state()
        if self._is_terminal():
            self._callbacks.persist_state()
            return True
        return False

    def _is_terminal(self) -> bool:
        rows = self._task_store.list_rows(self.verification_id)
        return bool(rows) and all(
            str(row["status"])
            in {
                VerificationTaskStore.TASK_DONE,
                VerificationTaskStore.TASK_FAILED,
                VerificationTaskStore.TASK_CANCELLED,
            }
            for row in rows
        )

    def _apply_fail_flag(self) -> bool:
        fail_flag, fail_reason = self._task_store.fail_state(self.verification_id)
        if not fail_flag:
            self._applied_fail_reason = ""
            self._cancel_reason = ""
            return False
        cancel_reason = fail_reason or "cancelled after main-correct failure"
        if cancel_reason == self._applied_fail_reason:
            return False
        if self._cancel_reason and cancel_reason == self._cancel_reason and is_cancel_reason(cancel_reason):
            self._applied_fail_reason = cancel_reason
            return False
        self._callbacks.cancel_queued_tasks(cancel_reason)
        self._task_store.cancel_not_started_tasks(self.verification_id, reason=cancel_reason)
        self._applied_fail_reason = cancel_reason
        return True

    def _publish_ready_rows(self) -> bool:
        if self._task_store.fail_state(self.verification_id)[0]:
            return False
        changed = False
        published_count = 0
        while True:
            rows = self._task_store.list_rows(self.verification_id)
            ready_rows = _ready_rows(rows, self._edges)
            if not ready_rows:
                break
            for row in ready_rows:
                published = self._callbacks.publish_task(row)
                task_id = str(row["id"])
                if published.terminal_result is None:
                    self._task_store.set_task_queued(
                        task_id,
                        run_id=published.run_id,
                        judgehost_task_id=published.judgehost_task_id,
                    )
                else:
                    _save_result(
                        verification_id=self.verification_id,
                        task_store=self._task_store,
                        result=published.terminal_result,
                    )
                changed = True
                published_count += 1
                if published_count >= _PUBLISH_READY_BATCH_SIZE:
                    self.enqueue_bootstrap()
                    return changed
            if self._task_store.fail_state(self.verification_id)[0]:
                break
        return changed

    def _mark_case_leased(self, judgehost_task_id: str, test_name: str) -> bool:
        row = self._task_store.find_runtime_row_by_judgehost_case(judgehost_task_id, test_name)
        if row is None:
            return False
        if str(row["status"]) != VerificationTaskStore.TASK_QUEUED:
            return False
        self._task_store.set_task_leased(str(row["id"]))
        return True

    def _finalize_case_result(
        self,
        judgehost_task_id: str,
        test_name: str,
        result: dict[str, object] | None,
    ) -> bool:
        if result is None:
            return False
        row = self._task_store.find_runtime_row_by_judgehost_case(judgehost_task_id, test_name)
        if row is None:
            return False
        final_result = result
        _save_result(
            verification_id=self.verification_id,
            task_store=self._task_store,
            result=final_result["final_result"] if "final_result" in final_result else final_result,
        )
        return True

    def _finalize_terminal_task(self, judgehost_task_id: str) -> bool:
        if not judgehost_task_id:
            return False
        changed = False
        for row in self._task_store.find_runtime_rows_by_judgehost_task_id(judgehost_task_id):
            status = str(row["status"])
            if status not in {VerificationTaskStore.TASK_QUEUED, VerificationTaskStore.TASK_LEASED}:
                continue
            result = self._callbacks.resolve_case_result(judgehost_task_id, str(row["test_name"]))
            if result is None:
                continue
            from .task_result_finalize import finalize_verification_task_result

            final_result = finalize_verification_task_result(row, result=result)
            _save_result(
                verification_id=self.verification_id,
                task_store=self._task_store,
                result=final_result,
            )
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
    result: TaskExecutionResult,
) -> bool:
    coordinator = _runtime_coordinator(verification_id)
    if coordinator is None:
        return False
    coordinator.enqueue_case_reported(
        judgehost_task_id,
        test_name,
        {"final_result": result},
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
