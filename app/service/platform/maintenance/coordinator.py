"""Single-process coordination for exclusive maintenance operations."""

import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal, Protocol

from app.db import now_iso
from app.service.platform.maintenance.admission import MaintenanceAdmissionGate

logger = logging.getLogger(__name__)

MaintenanceStatus = Literal["idle", "running", "succeeded", "failed"]
MaintenanceOperation = Literal[
    "",
    "artifact_cleanup",
    "source_backup",
    "restart",
]


class WorkerMaintenancePort(Protocol):
    def active_counts(self) -> dict[str, int]: ...


class JudgehostMaintenancePort(Protocol):
    def busy_counts(self) -> dict[str, int]: ...


class MaintenanceOperationPort(Protocol):
    def run(
        self,
        *,
        operation_id: str,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class MaintenanceStart:
    accepted: bool
    reason: str
    busy: dict[str, int]


@dataclass
class MaintenanceSnapshot:
    status: MaintenanceStatus = "idle"
    operation: MaintenanceOperation = ""
    stage: str = ""
    operation_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    actor_user_id: int | None = None
    result: dict[str, object] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "operation": self.operation,
            "stage": self.stage,
            "operation_id": self.operation_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "actor_user_id": self.actor_user_id,
            "result": dict(self.result),
            "error": self.error,
        }


class MaintenanceCoordinator:
    """Own one active operation and its process-local status snapshot."""

    def __init__(
        self,
        *,
        admission_gate: MaintenanceAdmissionGate,
        cleanup_service: MaintenanceOperationPort,
        source_backup_service: MaintenanceOperationPort,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
        restart_process: Callable[[], None] | None = None,
    ) -> None:
        self._gate = admission_gate
        self._cleanup = cleanup_service
        self._source_backup = source_backup_service
        self._worker_queue = worker_queue_service
        self._judgehost = judgehost_task_service
        self._restart_process = restart_process or (lambda: os._exit(0))
        self._snapshot = MaintenanceSnapshot()
        self._worker: threading.Thread | None = None

    @contextmanager
    def problem_deletion_guard(self) -> Iterator[None]:
        """Exclude runtime work while one problem is deleted."""

        with self._gate.locked():
            if not self._gate.is_open_locked():
                raise RuntimeError("maintenance in progress")
            busy = self._busy_snapshot_locked()
            runtime_busy = {
                name: count
                for name, count in busy.items()
                if name != "inflight_requests"
            }
            if any(count > 0 for count in runtime_busy.values()):
                raise ValueError("cannot delete problem while runtime jobs are active")
            yield

    def snapshot(self, *, exclude_current_request: bool = False) -> dict[str, object]:
        with self._gate.locked():
            payload = self._snapshot.as_dict()
            payload["admission_state"] = self._gate.state_locked()
            busy = self._busy_snapshot_locked()
            if exclude_current_request and busy["inflight_requests"] > 0:
                busy["inflight_requests"] -= 1
            payload["busy"] = busy
            payload["active_requests"] = busy["inflight_requests"]
            return payload

    def begin_drain(self) -> MaintenanceStart:
        """Reject new business work while admitted jobs finish normally."""

        with self._gate.locked():
            if self._snapshot.status == "running":
                return MaintenanceStart(False, "already_running", {})
            try:
                busy = self._busy_snapshot_locked()
            except Exception as exc:
                return MaintenanceStart(False, f"admission_failed: {exc}", {})
            self._gate.drain_locked()
            return MaintenanceStart(True, "draining", busy)

    def cancel_drain(self) -> MaintenanceStart:
        with self._gate.locked():
            if self._snapshot.status == "running":
                return MaintenanceStart(False, "already_running", {})
            self._gate.open_locked()
            return MaintenanceStart(True, "open", self._busy_snapshot_locked())

    def restart_when_idle(self, *, actor_user_id: int) -> MaintenanceStart:
        """Exit only after the explicitly drained runtime reaches idle."""

        with self._gate.locked():
            if self._snapshot.status == "running":
                return MaintenanceStart(False, "already_running", {})
            if not self._gate.is_draining_locked():
                return MaintenanceStart(False, "drain_required", {})
            try:
                busy = self._busy_snapshot_locked()
            except Exception as exc:
                return MaintenanceStart(False, f"admission_failed: {exc}", {})
            if any(busy.values()):
                return MaintenanceStart(False, "busy", busy)
            self._gate.close_locked()
            operation_id = f"restart-{uuid.uuid4().hex}"
            started_at = now_iso()
            self._snapshot = MaintenanceSnapshot(
                status="running",
                operation="restart",
                stage="exiting",
                operation_id=operation_id,
                started_at=started_at,
                actor_user_id=int(actor_user_id),
            )
            try:
                restart_thread = threading.Thread(
                    target=self._restart_after_response,
                    daemon=True,
                    name="application-restart",
                )
                restart_thread.start()
            except Exception as exc:
                self._snapshot.status = "failed"
                self._snapshot.finished_at = now_iso()
                self._snapshot.error = str(exc)
                self._gate.drain_locked()
                return MaintenanceStart(False, "restart_thread_failed", {})
            return MaintenanceStart(True, "restarting", {})

    def _restart_after_response(self) -> None:
        """Give the redirect time to leave the socket, then exit the process."""

        threading.Event().wait(0.5)
        try:
            self._restart_process()
        except Exception as exc:
            logger.exception("application restart exit failed")
            with self._gate.locked():
                self._snapshot.status = "failed"
                self._snapshot.finished_at = now_iso()
                self._snapshot.error = str(exc)
                self._gate.drain_locked()

    def _busy_snapshot_locked(self) -> dict[str, int]:
        worker = self._worker_queue.active_counts()
        judgehost = self._judgehost.busy_counts()
        return {
            "worker_queued": int(worker.get("queued", 0)),
            "worker_running": int(worker.get("running", 0)),
            "judgehost_queued": int(judgehost.get("queued", 0)),
            "judgehost_leased": int(judgehost.get("leased", 0)),
            "judgehost_reporting": int(judgehost.get("reporting", 0)),
            "judgehost_callbacks": int(judgehost.get("callbacks", 0)),
            "judgehost_cache_probes": int(judgehost.get("cache_probes", 0)),
            "judgehost_materializations": int(judgehost.get("materializations", 0)),
            "judgehost_finalizations": int(judgehost.get("finalizations", 0)),
            "inflight_requests": self._gate.active_requests_locked(),
        }

    def start_cleanup(self, *, actor_user_id: int) -> MaintenanceStart:
        return self._start_operation(
            actor_user_id=actor_user_id,
            operation="artifact_cleanup",
            operation_id_prefix="cleanup",
            thread_name="artifact-cleanup",
            service=self._cleanup,
        )

    def start_source_backup(self, *, actor_user_id: int) -> MaintenanceStart:
        return self._start_operation(
            actor_user_id=actor_user_id,
            operation="source_backup",
            operation_id_prefix="backup",
            thread_name="source-backup",
            service=self._source_backup,
        )

    def _start_operation(
        self,
        *,
        actor_user_id: int,
        operation: MaintenanceOperation,
        operation_id_prefix: str,
        thread_name: str,
        service: MaintenanceOperationPort,
    ) -> MaintenanceStart:
        with self._gate.locked():
            if self._snapshot.status == "running":
                return MaintenanceStart(False, "already_running", {})
            if not self._gate.is_draining_locked():
                return MaintenanceStart(False, "drain_required", {})
            previous_state = self._gate.state_locked()
            self._gate.close_locked()
            try:
                busy = self._busy_snapshot_locked()
            except Exception as exc:
                self._gate.restore_locked(previous_state)
                return MaintenanceStart(False, f"admission_failed: {exc}", {})
            if any(value > 0 for value in busy.values()):
                self._gate.restore_locked(previous_state)
                return MaintenanceStart(False, "busy", busy)
            operation_id = f"{operation_id_prefix}-{uuid.uuid4().hex}"
            started_at = now_iso()
            self._snapshot = MaintenanceSnapshot(
                status="running",
                operation=operation,
                stage="starting",
                operation_id=operation_id,
                started_at=started_at,
                actor_user_id=int(actor_user_id),
            )

        thread_error: Exception | None = None
        with self._gate.locked():
            try:
                self._worker = threading.Thread(
                    target=self._run_operation,
                    args=(service, operation_id, started_at),
                    daemon=True,
                    name=thread_name,
                )
                self._worker.start()
            except Exception as exc:
                self._snapshot.stage = "thread_start"
                thread_error = exc
        if thread_error is None:
            return MaintenanceStart(True, "started", {})

        details: dict[str, object] = {
            "operation_id": operation_id,
            "started_at": started_at,
            "finished_at": now_iso(),
            "completed_stage": "admission",
            "failed_stage": "thread_start",
            "error": str(thread_error),
        }
        logger.exception("maintenance thread failed to start", exc_info=thread_error)
        with self._gate.locked():
            self._snapshot.status = "failed"
            self._snapshot.finished_at = str(details["finished_at"])
            self._snapshot.result = details
            self._snapshot.error = str(thread_error)
            self._gate.restore_locked(previous_state)
        return MaintenanceStart(False, "thread_start_failed", {})

    def _run_operation(
        self,
        service: MaintenanceOperationPort,
        operation_id: str,
        started_at: str,
    ) -> None:
        def set_stage(stage: str) -> None:
            with self._gate.locked():
                self._snapshot.stage = stage

        try:
            result = service.run(
                operation_id=operation_id,
                started_at=started_at,
                set_stage=set_stage,
            )
        except Exception as exc:
            failure_result = getattr(exc, "maintenance_result", {})
            with self._gate.locked():
                self._snapshot.status = "failed"
                self._snapshot.finished_at = now_iso()
                self._snapshot.error = str(exc)
                self._snapshot.result = (
                    dict(failure_result) if isinstance(failure_result, dict) else {}
                )
                self._gate.open_locked()
            return
        with self._gate.locked():
            self._snapshot.status = "succeeded"
            self._snapshot.stage = "complete"
            self._snapshot.finished_at = str(result.get("finished_at") or now_iso())
            self._snapshot.result = result
            self._snapshot.error = ""
            self._gate.open_locked()
