"""Site-wide source-state backup for bare repositories and workspaces."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, TypedDict

from app.db import DB, now_iso
from app.service.platform.maintenance import validate_storage_layout
from app.setting import Settings


SOURCE_BACKUP_DIRECTORY = "source-backup"
SOURCE_BACKUP_ARCHIVE = "latest.tar.gz"
SOURCE_BACKUP_DOWNLOAD_NAME = "polygon-replica-source-backup.tar.gz"

_SOURCE_ROOTS = (
    ("bare_root", "bare"),
    ("workspace_root", "workspaces"),
)


class SourceBackupSummary(TypedDict):
    """Public metadata for the single downloadable backup."""

    available: bool
    filename: str
    size_bytes: int
    created_at: str


class SourceBackupPreflight(TypedDict):
    """Validated paths and bounded summary data used by one backup run."""

    roots: dict[str, Path]
    source_stats: dict[str, dict[str, int]]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _archive_directory_entry(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = 0o700
    info.mtime = int(time.time())
    archive.addfile(info)


class SourceBackupService:
    """Create and expose one atomic backup of both source-state roots."""

    def __init__(self, db: DB, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    @property
    def backup_directory(self) -> Path:
        """Return the application-owned backup directory."""

        return self._settings.backup_root / SOURCE_BACKUP_DIRECTORY

    @property
    def latest_path(self) -> Path:
        """Return the fixed path of the only published archive."""

        return self.backup_directory / SOURCE_BACKUP_ARCHIVE

    def configured_roots(self) -> dict[str, object]:
        """Describe backup sources and destination for the audit record."""

        return {
            "bare_root": str(self._settings.bare_root.absolute()),
            "workspace_root": str(self._settings.workspace_root.absolute()),
            "destination": str(self.latest_path.absolute()),
        }

    def _write_audit(
        self,
        actor_user_id: int,
        action: str,
        details: dict[str, object],
    ) -> int:
        def transaction(connection) -> int:
            cursor = connection.execute(
                """
                INSERT INTO audit_log(
                    actor_user_id,problem_id,action,details_json,created_at
                ) VALUES(?,NULL,?,?,?)
                """,
                (
                    int(actor_user_id),
                    action,
                    json.dumps(
                        details,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
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
        """Record backup admission before the background thread starts."""

        return self._write_audit(
            actor_user_id,
            "source_backup.start",
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
        """Record a terminal backup failure."""

        return self._write_audit(
            actor_user_id,
            "source_backup.failed",
            details,
        )

    @staticmethod
    def _inspect_tree(root: Path) -> tuple[int, int]:
        entry_count = 0
        total_bytes = 0

        def walk_error(exc: OSError) -> None:
            raise RuntimeError(
                f"cannot inspect backup source safely: {root}: {exc}"
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
                mode = child.lstat().st_mode
                entry_count += 1
                if stat.S_ISLNK(mode):
                    continue
                if child.is_mount():
                    raise RuntimeError(
                        f"backup source contains a nested mount point: {child}"
                    )
                if not stat.S_ISDIR(mode):
                    raise RuntimeError(
                        f"backup source contains an unsupported entry: {child}"
                    )
                traversable.append(name)
            directory_names[:] = traversable
            for name in filenames:
                child = parent / name
                child_stat = child.lstat()
                if child.is_mount():
                    raise RuntimeError(
                        f"backup source contains a nested mount point: {child}"
                    )
                if not (
                    stat.S_ISREG(child_stat.st_mode)
                    or stat.S_ISLNK(child_stat.st_mode)
                ):
                    raise RuntimeError(
                        f"backup source contains an unsupported entry: {child}"
                    )
                entry_count += 1
                if stat.S_ISREG(child_stat.st_mode):
                    total_bytes += int(child_stat.st_size)
        return entry_count, total_bytes

    def preflight(self) -> SourceBackupPreflight:
        """Validate both source trees and the application-owned destination."""

        roots = validate_storage_layout(self._settings)
        source_stats: dict[str, dict[str, int]] = {}
        for setting_name, archive_name in _SOURCE_ROOTS:
            root = roots[setting_name]
            if not root.exists() or not root.is_dir() or root.is_symlink():
                raise RuntimeError(f"backup source is unavailable: {setting_name}")
            entry_count, total_bytes = self._inspect_tree(root)
            source_stats[archive_name] = {
                "entries": entry_count,
                "bytes": total_bytes,
            }

        backup_root = roots["backup_root"]
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_directory = self.backup_directory
        if backup_directory.is_symlink():
            raise RuntimeError(
                f"source backup directory must not be a symlink: {backup_directory}"
            )
        if backup_directory.exists() and not backup_directory.is_dir():
            raise RuntimeError(
                f"source backup directory must be a directory: {backup_directory}"
        )
        backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        latest = self.latest_path
        if latest.is_symlink() or (latest.exists() and not latest.is_file()):
            raise RuntimeError(
                f"published source backup must be a regular file: {latest}"
            )
        return {
            "roots": roots,
            "source_stats": source_stats,
        }

    @staticmethod
    def _tar_filter(
        root: Path,
        archive_name: str,
    ) -> Callable[[tarfile.TarInfo], tarfile.TarInfo]:
        def filter_member(info: tarfile.TarInfo) -> tarfile.TarInfo:
            member_path = PurePosixPath(info.name)
            relative_parts = member_path.parts[1:]
            if not relative_parts:
                raise RuntimeError(
                    f"invalid source backup member path: {info.name}"
                )
            source = root.joinpath(*relative_parts)
            if source != root and source.is_mount() and not source.is_symlink():
                raise RuntimeError(
                    f"backup source contains a nested mount point: {source}"
                )
            if not (
                info.isfile()
                or info.isdir()
                or info.issym()
                or info.islnk()
            ):
                raise RuntimeError(
                    f"backup source contains an unsupported entry: {source}"
                )
            relative = PurePosixPath(*relative_parts).as_posix()
            info.name = f"{archive_name}/{relative}".rstrip("/")
            if info.isdir():
                info.name += "/"
            return info

        return filter_member

    def _write_archive(
        self,
        destination: Path,
        *,
        operation_id: str,
        started_at: str,
        roots: dict[str, Path],
        source_stats: dict[str, dict[str, int]],
    ) -> None:
        manifest = json.dumps(
            {
                "operation_id": operation_id,
                "created_at": started_at,
                "contents": [archive_name for _setting, archive_name in _SOURCE_ROOTS],
                "source_stats": source_stats,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        with tarfile.open(
            destination,
            mode="w:gz",
            format=tarfile.PAX_FORMAT,
            dereference=False,
        ) as archive:
            info = tarfile.TarInfo("manifest.json")
            info.mode = 0o600
            info.mtime = int(time.time())
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
            for setting_name, archive_name in _SOURCE_ROOTS:
                root = roots[setting_name]
                _archive_directory_entry(archive, archive_name)
                for child in sorted(root.iterdir(), key=lambda item: item.name):
                    archive.add(
                        child,
                        arcname=f"{archive_name}/{child.name}",
                        recursive=True,
                        filter=self._tar_filter(root, archive_name),
                    )
        descriptor = os.open(str(destination), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_stale_staging(self) -> None:
        for child in self.backup_directory.iterdir():
            if not (
                child.name.startswith(".source-backup-")
                and (
                    child.name.endswith(".partial")
                    or child.name.endswith(".previous")
                )
            ):
                continue
            if child.is_symlink() or not child.is_file():
                raise RuntimeError(f"invalid source backup staging entry: {child}")
            child.unlink()

    def _publish_archive(self, partial: Path) -> Path | None:
        """Publish one archive and retain a hard-linked rollback copy."""

        rollback: Path | None = None
        if self.latest_path.exists():
            rollback = self.backup_directory / (
                f".source-backup-{uuid.uuid4().hex}.previous"
            )
            os.link(self.latest_path, rollback)
            _fsync_directory(self.backup_directory)

        published = False
        try:
            os.replace(partial, self.latest_path)
            published = True
            _fsync_directory(self.backup_directory)
            return rollback
        except Exception:
            if published:
                self._restore_previous(rollback)
            elif rollback is not None:
                rollback.unlink(missing_ok=True)
            raise

    def _restore_previous(self, rollback: Path | None) -> None:
        if rollback is None:
            self.latest_path.unlink(missing_ok=True)
        else:
            os.replace(rollback, self.latest_path)
        _fsync_directory(self.backup_directory)

    def run(
        self,
        *,
        actor_user_id: int,
        operation_id: str,
        start_audit_id: int,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]:
        """Build and atomically publish the single latest source backup."""

        started = time.monotonic()
        stage = "preflight"
        result: dict[str, object] = {
            "operation_id": operation_id,
            "start_audit_id": int(start_audit_id),
            "started_at": started_at,
            "completed_stage": "admission",
        }
        partial = self.backup_directory / (
            f".source-backup-{uuid.uuid4().hex}.tar.gz.partial"
        )
        rollback: Path | None = None
        published = False
        try:
            set_stage(stage)
            preflight = self.preflight()
            roots = preflight["roots"]
            source_stats = preflight["source_stats"]
            result["source_stats"] = source_stats
            result["completed_stage"] = "preflight"
            self._remove_stale_staging()

            stage = "archive"
            set_stage(stage)
            self._write_archive(
                partial,
                operation_id=operation_id,
                started_at=started_at,
                roots=roots,
                source_stats=source_stats,
            )
            result["archive_bytes"] = int(partial.stat().st_size)
            result["completed_stage"] = "archive"

            stage = "publish"
            set_stage(stage)
            rollback = self._publish_archive(partial)
            published = True
            result["completed_stage"] = "publish"
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )

            stage = "audit"
            set_stage(stage)
            self._write_audit(
                actor_user_id,
                "source_backup.succeeded",
                result,
            )
            published = False
            if rollback is not None:
                try:
                    rollback.unlink(missing_ok=True)
                    _fsync_directory(self.backup_directory)
                except OSError:
                    # The published archive and terminal audit are authoritative.
                    # A later run removes this hidden hard-linked staging entry.
                    pass
            return result
        except Exception as exc:
            partial.unlink(missing_ok=True)
            if published:
                try:
                    self._restore_previous(rollback)
                except Exception as rollback_exc:
                    exc = RuntimeError(
                        f"{exc}; cannot restore previous source backup: "
                        f"{rollback_exc}"
                    )
            elif rollback is not None:
                rollback.unlink(missing_ok=True)
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )
            result["failed_stage"] = stage
            result["error"] = str(exc)
            try:
                self.write_failed_audit(
                    actor_user_id=actor_user_id,
                    details=result,
                )
            except Exception as audit_exc:
                combined = RuntimeError(
                    f"source backup failed at {stage}: {exc}; "
                    f"terminal audit failed: {audit_exc}"
                )
                setattr(combined, "maintenance_result", result)
                raise combined from audit_exc
            setattr(exc, "maintenance_result", result)
            raise

    def latest_archive_path(self) -> Path | None:
        """Resolve the latest regular archive without following a symlink."""

        candidate = self.latest_path
        if (
            candidate.is_symlink()
            or not candidate.exists()
            or not candidate.is_file()
        ):
            return None
        return candidate

    def latest_summary(self) -> SourceBackupSummary:
        """Return bounded metadata derived from the single archive file."""

        candidate = self.latest_archive_path()
        if candidate is None:
            return {
                "available": False,
                "filename": SOURCE_BACKUP_DOWNLOAD_NAME,
                "size_bytes": 0,
                "created_at": "",
            }
        file_stat = candidate.stat()
        return {
            "available": True,
            "filename": SOURCE_BACKUP_DOWNLOAD_NAME,
            "size_bytes": int(file_stat.st_size),
            "created_at": datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        }
