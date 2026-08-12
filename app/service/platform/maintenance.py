"""Process-local coordination for exclusive site maintenance operations."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol, TypedDict

from app.db import (
    DB,
    current_index_statements_for_tables,
    current_schema_statements_for_tables,
    now_iso,
)
from app.service.platform.admission import MaintenanceAdmissionGate
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.platform.fs.layout import StorageLayout


logger = logging.getLogger(__name__)


MaintenanceStatus = Literal["idle", "running", "succeeded", "failed"]
MaintenanceOperation = Literal["", "artifact_cleanup", "source_backup"]

class WorkerMaintenancePort(Protocol):
    def active_counts(self) -> dict[str, int]: ...

    def reset_runtime_history(self) -> None: ...


class JudgehostMaintenancePort(Protocol):
    def busy_counts(self) -> dict[str, int]: ...

    def reset_runtime_state(self) -> None: ...


class VerificationTaskMaintenancePort(Protocol):
    def reset_runtime_state(self) -> None: ...


class MaintenanceOperationPort(Protocol):
    """Operation hooks executed while the shared admission gate is closed."""

    def run(
        self,
        *,
        operation_id: str,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]: ...


class ArtifactUsageSnapshot(TypedDict):
    artifacts_bytes: int
    artifacts_files: int
    cache_bytes: int
    cache_files: int
    total_bytes: int
    total_files: int
    artifact_rows: int
    removable_rows: int
    table_rows: dict[str, int]


def _assert_cleanup_tree_safe(root: Path) -> None:
    """Reject links and nested mounts below a recursively deleted root."""

    if not root.exists():
        return

    def walk_error(exc: OSError) -> None:
        raise RuntimeError(
            f"cannot inspect cleanup root safely: {root}: {exc}"
        ) from exc

    for directory, directory_names, filenames in os.walk(
        root,
        topdown=True,
        onerror=walk_error,
        followlinks=False,
    ):
        parent = Path(directory)
        traversable: list[str] = []
        for name in directory_names:
            child = parent / name
            if child.is_symlink():
                raise RuntimeError(
                    f"cleanup root contains a symbolic link: {child}"
                )
            if child.is_mount():
                raise RuntimeError(
                    f"cleanup root contains a nested mount point: {child}"
                )
            traversable.append(name)
        directory_names[:] = traversable
        for name in filenames:
            child = parent / name
            if child.is_symlink():
                raise RuntimeError(
                    f"cleanup root contains a symbolic link: {child}"
                )
            if child.is_mount():
                raise RuntimeError(
                    f"cleanup root contains a nested mount point: {child}"
                )


def validate_runtime_startup_preconditions(
    storage_layout: StorageLayout,
) -> dict[str, Path]:
    """Reject unsafe roots before runtime services touch the startup-cleared cache."""

    roots = storage_layout.validate()
    _assert_cleanup_tree_safe(roots["cache_root"])
    return roots


def validate_cleanup_preconditions(storage_layout: StorageLayout) -> dict[str, Path]:
    """Revalidate both recursively deleted trees immediately before cleanup."""

    roots = validate_runtime_startup_preconditions(storage_layout)
    _assert_cleanup_tree_safe(roots["artifacts_root"])
    return roots


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


class ArtifactCleanupService:
    """Delete derived database rows and filesystem payloads."""

    _ARTIFACT_TABLES = (
        "previews",
        "contest_build_items",
        "contest_artifacts",
        "export_jobs",
        "exports",
        "problem_package_builds",
        "problem_package_materializations",
        "contest_jobs",
        "verification_artifact_refs",
        "verification_selected_tests",
        "verification_source_paths",
        "verification_sanity_check_messages",
        "verification_sanity_checks",
        "verification_tests_meta",
        "verification_task_diagnostics",
        "verification_tasks",
        "verifications",
    )

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        runtime_cache_index: RuntimeCacheIndex,
        runtime_blob_store: RuntimeBlobStore,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
        verification_task_store: VerificationTaskMaintenancePort,
        reset_process_job_tracking: Callable[[], None],
    ) -> None:
        self._db = db
        self._storage_layout = storage_layout
        self._runtime_cache_index = runtime_cache_index
        self._runtime_blob_store = runtime_blob_store
        self._worker_queue = worker_queue_service
        self._judgehost = judgehost_task_service
        self._verification_task_store = verification_task_store
        self._reset_process_job_tracking = reset_process_job_tracking

    def preflight(
        self,
        *,
        create_cleanup_roots: bool = False,
    ) -> dict[str, object]:
        roots = validate_cleanup_preconditions(self._storage_layout)
        if create_cleanup_roots:
            for root in (
                roots["artifacts_root"],
                roots["cache_root"],
            ):
                root.mkdir(parents=True, exist_ok=True)
        result = {name: str(root) for name, root in roots.items()}
        result["database"] = str(self._storage_layout.database_path.absolute().resolve())
        return result

    def _delete_metadata(self) -> dict[str, int]:
        def transaction(connection) -> dict[str, int]:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in self._ARTIFACT_TABLES
            }
            for table in self._ARTIFACT_TABLES:
                connection.execute(f"DROP TABLE {table}")
            for statement in current_schema_statements_for_tables(
                self._ARTIFACT_TABLES
            ):
                connection.execute(statement)
            for statement in current_index_statements_for_tables(
                self._ARTIFACT_TABLES
            ):
                connection.execute(statement)
            return counts

        return self._db.write_schema_reset_transaction(transaction)

    @staticmethod
    def _tree_usage(root: Path) -> tuple[int, int]:
        total = 0
        files = 0
        if not root.exists() or root.is_symlink():
            return 0, 0
        for directory, _dirnames, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    if not path.is_symlink():
                        total += int(path.stat().st_size)
                        files += 1
                except OSError:
                    continue
        return total, files

    @classmethod
    def _tree_bytes(cls, root: Path) -> int:
        total, _files = cls._tree_usage(root)
        return total

    def usage_snapshot(self) -> ArtifactUsageSnapshot:
        artifacts_bytes, artifacts_files = self._tree_usage(
            self._storage_layout.artifacts_root.absolute()
        )
        cache_bytes, cache_files = self._tree_usage(
            self._storage_layout.cache_root.absolute()
        )
        count_expressions = [
            f"(SELECT COUNT(*) FROM {table}) AS {table}"
            for table in self._ARTIFACT_TABLES
        ]
        row = self._db.fetch_one("SELECT " + ", ".join(count_expressions))
        if row is None:
            raise RuntimeError("artifact usage query returned no row")
        table_rows = {
            table: int(row[table])
            for table in self._ARTIFACT_TABLES
        }
        artifact_rows = sum(table_rows.values())
        return {
            "artifacts_bytes": artifacts_bytes,
            "artifacts_files": artifacts_files,
            "cache_bytes": cache_bytes,
            "cache_files": cache_files,
            "total_bytes": artifacts_bytes + cache_bytes,
            "total_files": artifacts_files + cache_files,
            "artifact_rows": artifact_rows,
            "removable_rows": artifact_rows,
            "table_rows": table_rows,
        }

    @staticmethod
    def _clear_root(root: Path) -> int:
        if root.is_symlink():
            raise RuntimeError(f"cleanup root must not be a symlink: {root}")
        root.mkdir(parents=True, exist_ok=True)
        _assert_cleanup_tree_safe(root)
        before = ArtifactCleanupService._tree_bytes(root)
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        root.mkdir(parents=True, exist_ok=True)
        return max(0, before - ArtifactCleanupService._tree_bytes(root))

    def _checkpoint_truncate(self) -> None:
        with self._db.conn() as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0:
            raise RuntimeError(f"SQLite WAL checkpoint remained busy: {row!r}")

    def _vacuum(self) -> None:
        self._checkpoint_truncate()
        with self._db.conn() as connection:
            connection.execute("VACUUM")
        self._checkpoint_truncate()

    def _database_storage_bytes(self) -> int:
        paths = (
            self._storage_layout.database_path,
            Path(f"{self._storage_layout.database_path}-wal"),
            Path(f"{self._storage_layout.database_path}-shm"),
        )
        total = 0
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink():
                    total += int(path.stat().st_size)
            except OSError:
                continue
        return total

    def run(
        self,
        *,
        operation_id: str,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]:
        started = time.monotonic()
        stage = "preflight"
        result: dict[str, object] = {
            "operation_id": operation_id,
            "started_at": started_at,
            "completed_stage": "admission",
            "deleted_rows": {},
            "deleted_row_total": 0,
            "affected_row_total": 0,
            "reclaimed_bytes": {},
            "total_reclaimed_bytes": 0,
        }
        database_bytes_before = self._database_storage_bytes()
        reclaimed_bytes: dict[str, int] = {}
        filesystem_bytes_before: dict[str, int] = {}
        result["database_bytes_before"] = database_bytes_before
        result["reclaimed_bytes"] = reclaimed_bytes

        def move(next_stage: str) -> None:
            nonlocal stage
            stage = next_stage
            set_stage(next_stage)

        try:
            move("preflight")
            result["roots"] = self.preflight(create_cleanup_roots=True)
            result["completed_stage"] = "preflight"
            move("database")
            deleted_rows = self._delete_metadata()
            result["deleted_rows"] = deleted_rows
            result["deleted_row_total"] = sum(deleted_rows.values())
            result["affected_row_total"] = result["deleted_row_total"]
            result["completed_stage"] = "database"
            move("filesystem")
            filesystem_bytes_before = {
                "artifacts_root": self._tree_bytes(
                    self._storage_layout.artifacts_root.absolute()
                ),
                "cache_root": self._tree_bytes(
                    self._storage_layout.cache_root.absolute()
                ),
            }
            result["filesystem_bytes_before"] = dict(filesystem_bytes_before)
            reclaimed_bytes["artifacts_root"] = self._clear_root(
                self._storage_layout.artifacts_root.absolute()
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            reclaimed_bytes["cache_root"] = self._clear_root(
                self._storage_layout.cache_root.absolute()
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            result["completed_stage"] = "filesystem"
            move("runtime")
            self._runtime_cache_index.clear_all()
            self._runtime_blob_store.clear_all()
            self._judgehost.reset_runtime_state()
            self._verification_task_store.reset_runtime_state()
            self._worker_queue.reset_runtime_history()
            self._reset_process_job_tracking()
            result["completed_stage"] = "runtime"
            move("vacuum")
            self._vacuum()
            database_bytes_after = self._database_storage_bytes()
            result["database_bytes_after"] = database_bytes_after
            reclaimed_bytes["sqlite"] = max(
                0,
                database_bytes_before - database_bytes_after,
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            result["completed_stage"] = "vacuum"
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(round((time.monotonic() - started) * 1000))
            logger.info("artifact cleanup succeeded", extra={"result": result})
            return result
        except Exception as exc:
            cleanup_roots = {
                "artifacts_root": self._storage_layout.artifacts_root.absolute(),
                "cache_root": self._storage_layout.cache_root.absolute(),
            }
            for label, before in filesystem_bytes_before.items():
                reclaimed_bytes[label] = max(
                    int(reclaimed_bytes.get(label, 0)),
                    int(before) - self._tree_bytes(cleanup_roots[label]),
                )
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(round((time.monotonic() - started) * 1000))
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            result["failed_stage"] = stage
            result["error"] = str(exc)
            logger.exception("artifact cleanup failed", extra={"result": result})
            setattr(exc, "cleanup_result", result)
            raise


class MaintenanceCoordinator:
    """Single-process boundary for mutually exclusive site maintenance."""

    _EXEMPT_PREFIXES = ("/api/v4/",)
    _EXEMPT_PATHS = frozenset(
        {
            "/api/v4",
            "/maintenance",
            "/admin/maintenance/artifacts/cleanup",
            "/admin/maintenance/source-backup",
        }
    )

    def __init__(
        self,
        cleanup_service: MaintenanceOperationPort,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
        source_backup_service: MaintenanceOperationPort,
    ) -> None:
        self._cleanup = cleanup_service
        self._source_backup = source_backup_service
        self._worker_queue = worker_queue_service
        self._judgehost = judgehost_task_service
        self._gate = MaintenanceAdmissionGate()
        self._active_requests = 0
        self._snapshot = MaintenanceSnapshot()
        self._worker: threading.Thread | None = None

    @property
    def admission_gate(self) -> MaintenanceAdmissionGate:
        return self._gate

    def is_exempt(self, path: str) -> bool:
        return path in self._EXEMPT_PATHS or any(
            path.startswith(prefix) for prefix in self._EXEMPT_PREFIXES
        )

    def enter_request(self) -> bool:
        with self._gate.locked():
            if not self._gate.is_open_locked():
                return False
            self._active_requests += 1
            return True

    def leave_request(self) -> None:
        with self._gate.locked():
            if self._active_requests <= 0:
                raise RuntimeError("maintenance request counter underflow")
            self._active_requests -= 1

    def allow_new_work(self) -> bool:
        return self._gate.is_open()

    @contextmanager
    def problem_deletion_guard(self) -> Iterator[None]:
        """Exclude new runtime work while one problem is deleted.

        The deleting HTTP request is already counted by ``enter_request`` and
        therefore cannot use the full maintenance busy snapshot verbatim.
        Worker and Judgehost work are the relevant process-local users of
        problem execution state.
        """

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
                raise ValueError(
                    "cannot delete problem while runtime jobs are active"
                )
            yield

    def snapshot(self) -> dict[str, object]:
        with self._gate.locked():
            payload = self._snapshot.as_dict()
            payload["active_requests"] = self._active_requests
            return payload

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
            "inflight_requests": int(self._active_requests),
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
        """Start the exclusive bare-repository and workspace backup."""

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
            self._gate.close_locked()
            try:
                busy = self._busy_snapshot_locked()
            except Exception as exc:
                self._gate.open_locked()
                return MaintenanceStart(False, f"admission_failed: {exc}", {})
            if any(value > 0 for value in busy.values()):
                self._gate.open_locked()
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
                    args=(
                        service,
                        operation_id,
                        started_at,
                    ),
                    daemon=True,
                    name=thread_name,
                )
                self._worker.start()
            except Exception as exc:
                self._snapshot.stage = "thread_start"
                thread_error = exc
        if thread_error is None:
            return MaintenanceStart(True, "started", {})

        details = {
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
            self._gate.open_locked()
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
            failure_result = getattr(
                exc,
                "maintenance_result",
                getattr(exc, "cleanup_result", {}),
            )
            with self._gate.locked():
                self._snapshot.status = "failed"
                self._snapshot.finished_at = now_iso()
                self._snapshot.error = str(exc)
                self._snapshot.result = (
                    dict(failure_result)
                    if isinstance(failure_result, dict)
                    else {}
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
