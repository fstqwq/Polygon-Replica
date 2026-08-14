"""Artifact-cleanup policy orchestration over explicit mechanics."""

import logging
import time
from typing import Callable, Protocol

from app.db import now_iso
from app.service.platform.maintenance.database import ArtifactCleanupDatabase
from app.service.platform.maintenance.filesystem import ArtifactCleanupFilesystem
from app.service.platform.maintenance.plan import (
    ARTIFACT_TABLES,
    CLEANUP_FILESYSTEM_CLASSES,
    REDUNDANT_DATABASE_INDEXES,
    ArtifactUsageSnapshot,
    CleanupFilesystemClass,
)
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


logger = logging.getLogger(__name__)


class WorkerMaintenancePort(Protocol):
    def reset_runtime_history(self) -> None: ...


class JudgehostMaintenancePort(Protocol):
    def reset_runtime_state(self) -> None: ...


class VerificationTaskMaintenancePort(Protocol):
    def reset_runtime_state(self) -> None: ...


class ArtifactCleanupService:
    """Apply the cleanup inventory in its documented destructive order."""

    def __init__(
        self,
        database: ArtifactCleanupDatabase,
        filesystem: ArtifactCleanupFilesystem,
        runtime_cache_index: RuntimeCacheIndex,
        runtime_blob_store: RuntimeBlobStore,
        worker_queue_service: WorkerMaintenancePort,
        judgehost_task_service: JudgehostMaintenancePort,
        verification_task_store: VerificationTaskMaintenancePort,
        reset_process_job_tracking: Callable[[], None],
    ) -> None:
        self._database = database
        self._filesystem = filesystem
        self._runtime_cache_index = runtime_cache_index
        self._runtime_blob_store = runtime_blob_store
        self._worker_queue = worker_queue_service
        self._judgehost = judgehost_task_service
        self._verification_task_store = verification_task_store
        self._reset_process_job_tracking = reset_process_job_tracking

    def usage_snapshot(self) -> ArtifactUsageSnapshot:
        roots = self._filesystem.roots(CLEANUP_FILESYSTEM_CLASSES)
        artifacts_bytes, artifacts_files = self._filesystem.tree_usage(
            roots["artifacts_root"]
        )
        cache_bytes, cache_files = self._filesystem.tree_usage(
            roots["cache_root"]
        )
        table_rows = self._database.table_counts(ARTIFACT_TABLES)
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
        database_bytes_before = self._database.storage_bytes()
        reclaimed_bytes: dict[str, int] = {}
        filesystem_bytes_before: dict[CleanupFilesystemClass, int] = {}
        result["database_bytes_before"] = database_bytes_before
        result["reclaimed_bytes"] = reclaimed_bytes

        def move(next_stage: str) -> None:
            nonlocal stage
            stage = next_stage
            set_stage(next_stage)

        try:
            move("preflight")
            result["roots"] = self._filesystem.preflight(
                CLEANUP_FILESYSTEM_CLASSES,
                create_roots=True,
            )
            result["completed_stage"] = "preflight"
            move("database")
            deleted_rows = self._database.reset_tables(
                ARTIFACT_TABLES,
                drop_indexes=REDUNDANT_DATABASE_INDEXES,
            )
            result["deleted_rows"] = deleted_rows
            result["deleted_row_total"] = sum(deleted_rows.values())
            result["affected_row_total"] = result["deleted_row_total"]
            result["completed_stage"] = "database"
            move("filesystem")
            cleanup_roots = self._filesystem.roots(CLEANUP_FILESYSTEM_CLASSES)
            filesystem_bytes_before = {
                label: self._filesystem.tree_bytes(root)
                for label, root in cleanup_roots.items()
            }
            result["filesystem_bytes_before"] = dict(filesystem_bytes_before)
            for label, root in cleanup_roots.items():
                reclaimed_bytes[label] = self._filesystem.clear_root(root)
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
            self._database.vacuum()
            database_bytes_after = self._database.storage_bytes()
            result["database_bytes_after"] = database_bytes_after
            reclaimed_bytes["sqlite"] = max(
                0,
                database_bytes_before - database_bytes_after,
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            result["completed_stage"] = "vacuum"
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )
            logger.info("artifact cleanup succeeded", extra={"result": result})
            return result
        except Exception as exc:
            for root_label, before in filesystem_bytes_before.items():
                root = self._filesystem.roots(CLEANUP_FILESYSTEM_CLASSES)[root_label]
                reclaimed_bytes[root_label] = max(
                    int(reclaimed_bytes.get(root_label, 0)),
                    int(before) - self._filesystem.tree_bytes(root),
                )
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )
            result["total_reclaimed_bytes"] = sum(reclaimed_bytes.values())
            result["failed_stage"] = stage
            result["error"] = str(exc)
            logger.exception("artifact cleanup failed", extra={"result": result})
            setattr(exc, "maintenance_result", result)
            raise
