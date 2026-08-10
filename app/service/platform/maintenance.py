"""Process-local maintenance coordination and destructive artifact cleanup."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol, TypedDict

from app.db import (
    DB,
    current_index_statements_for_tables,
    current_schema_statements_for_tables,
    now_iso,
)
from app.service.platform.admission import MaintenanceAdmissionGate
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.setting import Settings


MaintenanceStatus = Literal["idle", "running", "succeeded", "failed"]

_STORAGE_ROOT_ATTRIBUTES = (
    "bare_root",
    "workspace_root",
    "contest_source_root",
    "backup_root",
    "artifacts_root",
    "cache_root",
)


class WorkerMaintenancePort(Protocol):
    def active_counts(self) -> dict[str, int]: ...

    def reset_runtime_history(self) -> None: ...


class JudgehostMaintenancePort(Protocol):
    def busy_counts(self) -> dict[str, int]: ...

    def reset_runtime_state(self) -> None: ...


class VerificationTaskMaintenancePort(Protocol):
    def reset_runtime_state(self) -> None: ...


class ArtifactUsageSnapshot(TypedDict):
    artifacts_bytes: int
    artifacts_files: int
    cache_bytes: int
    cache_files: int
    total_bytes: int
    total_files: int
    artifact_rows: int
    audit_rows: int
    removable_rows: int
    table_rows: dict[str, int]


def _is_within(root: Path, target: Path) -> bool:
    return root == target or root in target.parents


def validate_storage_layout(settings: Settings) -> dict[str, Path]:
    """Validate configured root geometry before any runtime path is mutated."""

    configured = {
        name: Path(getattr(settings, name)).absolute()
        for name in _STORAGE_ROOT_ATTRIBUTES
    }
    for name, root in configured.items():
        if root == Path(root.anchor):
            raise RuntimeError(f"refusing filesystem root: {name}={root}")
        if root.is_symlink():
            raise RuntimeError(f"filesystem root must not be a symlink: {name}={root}")
        if root.exists() and not root.is_dir():
            raise RuntimeError(f"filesystem root must be a directory: {name}={root}")
    resolved = {name: root.resolve() for name, root in configured.items()}
    root_items = list(resolved.items())
    for index, (left_name, left) in enumerate(root_items):
        for right_name, right in root_items[index + 1 :]:
            if _is_within(left, right) or _is_within(right, left):
                raise RuntimeError(
                    f"filesystem roots overlap: {left_name}={left}, "
                    f"{right_name}={right}"
                )
    configured_database = settings.db_path.absolute()
    if configured_database.is_symlink():
        raise RuntimeError(f"database path must not be a symlink: {configured_database}")
    if configured_database.exists() and not configured_database.is_file():
        raise RuntimeError(f"database path must be a file: {configured_database}")
    database = configured_database.resolve()
    for name, root in resolved.items():
        if _is_within(root, database) or _is_within(database, root):
            raise RuntimeError(
                f"database path overlaps managed root: {name}={root}, "
                f"database={database}"
            )
    return resolved


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


def validate_runtime_startup_preconditions(settings: Settings) -> dict[str, Path]:
    """Reject unsafe roots before runtime services touch the startup-cleared cache."""

    roots = validate_storage_layout(settings)
    _assert_cleanup_tree_safe(roots["cache_root"])
    return roots


def validate_cleanup_preconditions(settings: Settings) -> dict[str, Path]:
    """Revalidate both recursively deleted trees immediately before cleanup."""

    roots = validate_runtime_startup_preconditions(settings)
    _assert_cleanup_tree_safe(roots["artifacts_root"])
    return roots


@dataclass(frozen=True)
class CleanupStart:
    accepted: bool
    reason: str
    busy: dict[str, int]


@dataclass
class MaintenanceSnapshot:
    status: MaintenanceStatus = "idle"
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
        "verification_tasks",
        "verifications",
    )

    def __init__(
        self,
        db: DB,
        settings: Settings,
        runtime_cache_index: RuntimeCacheIndex,
        runtime_blob_store: RuntimeBlobStore,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
        verification_task_store: VerificationTaskMaintenancePort,
        reset_process_job_tracking: Callable[[], None],
    ) -> None:
        self._db = db
        self._settings = settings
        self._runtime_cache_index = runtime_cache_index
        self._runtime_blob_store = runtime_blob_store
        self._worker_queue = worker_queue_service
        self._judgehost = judgehost_task_service
        self._verification_task_store = verification_task_store
        self._reset_process_job_tracking = reset_process_job_tracking

    def configured_roots(self) -> dict[str, object]:
        """Describe configured storage without performing cleanup preflight."""

        roots = {
            name: str(Path(getattr(self._settings, name)).absolute())
            for name in _STORAGE_ROOT_ATTRIBUTES
        }
        roots["database"] = str(self._settings.db_path.absolute())
        return roots

    def preflight(
        self,
        *,
        create_cleanup_roots: bool = False,
    ) -> dict[str, object]:
        roots = validate_cleanup_preconditions(self._settings)
        if create_cleanup_roots:
            for root in (
                roots["artifacts_root"],
                roots["cache_root"],
            ):
                root.mkdir(parents=True, exist_ok=True)
        result = {name: str(root) for name, root in roots.items()}
        result["database"] = str(self._settings.db_path.absolute().resolve())
        return result

    def _delete_metadata(self, *, start_audit_id: int) -> dict[str, int]:
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
            cursor = connection.execute(
                """
                UPDATE workspaces
                SET recent_verification_status=NULL, updated_at=?
                WHERE recent_verification_status IS NOT NULL
                """,
                (now_iso(),),
            )
            counts["workspace_statuses_cleared"] = max(0, int(cursor.rowcount))
            cursor = connection.execute(
                "DELETE FROM audit_log WHERE id < ?",
                (int(start_audit_id),),
            )
            counts["audit_log"] = max(0, int(cursor.rowcount))
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
            self._settings.artifacts_root.absolute()
        )
        cache_bytes, cache_files = self._tree_usage(
            self._settings.cache_root.absolute()
        )
        count_expressions = [
            f"(SELECT COUNT(*) FROM {table}) AS {table}"
            for table in self._ARTIFACT_TABLES
        ]
        count_expressions.append(
            "(SELECT COUNT(*) FROM audit_log) AS audit_log"
        )
        row = self._db.fetch_one("SELECT " + ", ".join(count_expressions))
        if row is None:
            raise RuntimeError("artifact usage query returned no row")
        table_rows = {
            table: int(row[table])
            for table in self._ARTIFACT_TABLES
        }
        artifact_rows = sum(table_rows.values())
        audit_rows = int(row["audit_log"])
        return {
            "artifacts_bytes": artifacts_bytes,
            "artifacts_files": artifacts_files,
            "cache_bytes": cache_bytes,
            "cache_files": cache_files,
            "total_bytes": artifacts_bytes + cache_bytes,
            "total_files": artifacts_files + cache_files,
            "artifact_rows": artifact_rows,
            "audit_rows": audit_rows,
            "removable_rows": artifact_rows + audit_rows,
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
            self._settings.db_path,
            Path(f"{self._settings.db_path}-wal"),
            Path(f"{self._settings.db_path}-shm"),
        )
        total = 0
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink():
                    total += int(path.stat().st_size)
            except OSError:
                continue
        return total

    def _write_audit(
        self,
        actor_user_id: int,
        action: str,
        details: dict[str, object],
    ) -> int:
        def transaction(connection) -> int:
            cursor = connection.execute(
                """
                INSERT INTO audit_log(actor_user_id, problem_id, action, details_json, created_at)
                VALUES (?, NULL, ?, ?, ?)
                """,
                (
                    int(actor_user_id),
                    action,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    now_iso(),
                ),
            )
            return int(cursor.lastrowid)

        return int(self._db.write_transaction(transaction))

    def write_start_audit(
        self,
        *,
        actor_user_id: int,
        operation_id: str,
        started_at: str,
        roots: dict[str, object],
    ) -> int:
        return self._write_audit(
            actor_user_id,
            "artifact_cleanup.start",
            {
                "operation_id": operation_id,
                "started_at": started_at,
                "roots": roots,
            },
        )

    def write_failed_audit(
        self,
        *,
        actor_user_id: int,
        details: dict[str, object],
    ) -> int:
        return self._write_audit(
            actor_user_id,
            "artifact_cleanup.failed",
            details,
        )

    def run(
        self,
        *,
        actor_user_id: int,
        operation_id: str,
        start_audit_id: int,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]:
        started = time.monotonic()
        stage = "preflight"
        result: dict[str, object] = {
            "operation_id": operation_id,
            "start_audit_id": int(start_audit_id),
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
            deleted_rows = self._delete_metadata(
                start_audit_id=start_audit_id
            )
            result["deleted_rows"] = deleted_rows
            result["deleted_row_total"] = sum(
                count
                for label, count in deleted_rows.items()
                if label != "workspace_statuses_cleared"
            )
            result["affected_row_total"] = sum(deleted_rows.values())
            result["completed_stage"] = "database"
            move("filesystem")
            filesystem_bytes_before = {
                "artifacts_root": self._tree_bytes(
                    self._settings.artifacts_root.absolute()
                ),
                "cache_root": self._tree_bytes(
                    self._settings.cache_root.absolute()
                ),
            }
            result["filesystem_bytes_before"] = dict(filesystem_bytes_before)
            reclaimed_bytes["artifacts_root"] = self._clear_root(
                self._settings.artifacts_root.absolute()
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            reclaimed_bytes["cache_root"] = self._clear_root(
                self._settings.cache_root.absolute()
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
            move("audit")
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(round((time.monotonic() - started) * 1000))
            self._write_audit(
                actor_user_id,
                "artifact_cleanup.succeeded",
                result,
            )
            return result
        except Exception as exc:
            cleanup_roots = {
                "artifacts_root": self._settings.artifacts_root.absolute(),
                "cache_root": self._settings.cache_root.absolute(),
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
            try:
                self.write_failed_audit(
                    actor_user_id=actor_user_id,
                    details=result,
                )
            except Exception as audit_exc:
                combined = RuntimeError(
                    f"artifact cleanup failed at {stage}: {exc}; "
                    f"terminal audit failed: {audit_exc}"
                )
                setattr(combined, "cleanup_result", result)
                raise combined from audit_exc
            setattr(exc, "cleanup_result", result)
            raise


class MaintenanceCoordinator:
    """Single-process state and shared admission boundary for cleanup."""

    _EXEMPT_PREFIXES = ("/api/v4/",)
    _EXEMPT_PATHS = frozenset(
        {"/api/v4", "/maintenance", "/admin/maintenance/artifacts/cleanup"}
    )

    def __init__(
        self,
        cleanup_service: ArtifactCleanupService,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
    ) -> None:
        self._cleanup = cleanup_service
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
            "inflight_requests": int(self._active_requests),
        }

    def start_cleanup(self, *, actor_user_id: int) -> CleanupStart:
        with self._gate.locked():
            if self._snapshot.status == "running":
                return CleanupStart(False, "already_running", {})
            self._gate.close_locked()
            try:
                busy = self._busy_snapshot_locked()
            except Exception as exc:
                self._gate.open_locked()
                return CleanupStart(False, f"admission_failed: {exc}", {})
            if any(value > 0 for value in busy.values()):
                self._gate.open_locked()
                return CleanupStart(False, "busy", busy)
            operation_id = f"cleanup-{uuid.uuid4().hex}"
            started_at = now_iso()
            self._snapshot = MaintenanceSnapshot(
                status="running",
                stage="start_audit",
                operation_id=operation_id,
                started_at=started_at,
                actor_user_id=int(actor_user_id),
            )

        try:
            start_audit_id = self._cleanup.write_start_audit(
                actor_user_id=int(actor_user_id),
                operation_id=operation_id,
                started_at=started_at,
                roots=self._cleanup.configured_roots(),
            )
        except Exception as exc:
            with self._gate.locked():
                self._snapshot = MaintenanceSnapshot(
                    status="failed",
                    stage="start_audit",
                    operation_id=operation_id,
                    started_at=started_at,
                    finished_at=now_iso(),
                    actor_user_id=int(actor_user_id),
                    result={
                        "completed_stage": "admission",
                        "failed_stage": "start_audit",
                    },
                    error=str(exc),
                )
                self._gate.open_locked()
            return CleanupStart(False, f"audit_failed: {exc}", busy)

        thread_error: Exception | None = None
        with self._gate.locked():
            self._snapshot.stage = "starting"
            self._snapshot.result = {"start_audit_id": int(start_audit_id)}
            try:
                self._worker = threading.Thread(
                    target=self._run_cleanup,
                    args=(
                        int(actor_user_id),
                        operation_id,
                        int(start_audit_id),
                        started_at,
                    ),
                    daemon=True,
                    name="artifact-cleanup",
                )
                self._worker.start()
            except Exception as exc:
                self._snapshot.stage = "thread_start"
                thread_error = exc
        if thread_error is None:
            return CleanupStart(True, "started", {})

        details = {
            "operation_id": operation_id,
            "start_audit_id": int(start_audit_id),
            "started_at": started_at,
            "finished_at": now_iso(),
            "completed_stage": "admission",
            "failed_stage": "thread_start",
            "error": str(thread_error),
        }
        audit_error = ""
        try:
            self._cleanup.write_failed_audit(
                actor_user_id=int(actor_user_id),
                details=details,
            )
        except Exception as audit_exc:
            audit_error = f"; terminal audit failed: {audit_exc}"
        with self._gate.locked():
            self._snapshot.status = "failed"
            self._snapshot.finished_at = str(details["finished_at"])
            self._snapshot.result = details
            self._snapshot.error = f"{thread_error}{audit_error}"
            self._gate.open_locked()
        return CleanupStart(False, "thread_start_failed", {})

    def _run_cleanup(
        self,
        actor_user_id: int,
        operation_id: str,
        start_audit_id: int,
        started_at: str,
    ) -> None:
        def set_stage(stage: str) -> None:
            with self._gate.locked():
                self._snapshot.stage = stage

        try:
            result = self._cleanup.run(
                actor_user_id=actor_user_id,
                operation_id=operation_id,
                start_audit_id=start_audit_id,
                started_at=started_at,
                set_stage=set_stage,
            )
        except Exception as exc:
            failure_result = getattr(exc, "cleanup_result", {})
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
