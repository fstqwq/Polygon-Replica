"""Site-wide source-state backup for bare repositories and workspaces."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, TypedDict

from app.db import now_iso
from app.service.platform.maintenance import validate_storage_layout
from app.setting import Settings


SOURCE_BACKUP_DIRECTORY = "source-backup"
SOURCE_BACKUP_ARCHIVE = "latest.tar.gz"
SOURCE_BACKUP_SIDECAR = "latest.tar.gz.sha256"
SOURCE_BACKUP_DOWNLOAD_NAME = "polygon-replica-source-backup.tar.gz"

logger = logging.getLogger(__name__)

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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def backup_directory(self) -> Path:
        """Return the application-owned backup directory."""

        return self._settings.backup_root / SOURCE_BACKUP_DIRECTORY

    @property
    def latest_path(self) -> Path:
        """Return the fixed path of the only published archive."""

        return self.backup_directory / SOURCE_BACKUP_ARCHIVE

    @property
    def sidecar_path(self) -> Path:
        """Return the digest sidecar paired with the published archive."""

        return self.backup_directory / SOURCE_BACKUP_SIDECAR

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
        sidecar = self.sidecar_path
        if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
            raise RuntimeError(
                f"source backup sidecar must be a regular file: {sidecar}"
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

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_sidecar(self, archive: Path, destination: Path) -> None:
        digest = self._sha256(archive)
        with destination.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{digest}  {SOURCE_BACKUP_ARCHIVE}\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _verify_archive_pair(self, archive_path: Path, sidecar_path: Path) -> None:
        sidecar_tokens = sidecar_path.read_text(encoding="ascii").split()
        if sidecar_tokens != [self._sha256(archive_path), SOURCE_BACKUP_ARCHIVE]:
            raise RuntimeError("source backup SHA-256 sidecar does not match archive")
        with tarfile.open(archive_path, mode="r:gz") as archive:
            names: set[str] = set()
            manifest: dict[str, object] | None = None
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise RuntimeError(
                        f"unsafe member in source backup archive: {member.name}"
                    )
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    raise RuntimeError(
                        f"unsupported member in source backup archive: {member.name}"
                    )
                names.add(member.name.rstrip("/"))
                if member.name == "manifest.json":
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError("source backup manifest is unreadable")
                    manifest = json.loads(stream.read().decode("ascii"))
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError(
                            f"source backup member is unreadable: {member.name}"
                        )
                    while stream.read(1024 * 1024):
                        pass
        if manifest is None or manifest.get("contents") != ["bare", "workspaces"]:
            raise RuntimeError("source backup manifest has invalid contents")
        if not {"manifest.json", "bare", "workspaces"}.issubset(names):
            raise RuntimeError("source backup archive is missing required roots")

    def _rollback_path(self, label: str) -> Path:
        return self.backup_directory / (
            f".source-backup-{uuid.uuid4().hex}.{label}.previous"
        )

    def _publish_archive(
        self,
        partial: Path,
        partial_sidecar: Path,
    ) -> tuple[Path | None, Path | None]:
        """Publish one verified archive/sidecar pair with rollback copies."""

        archive_rollback: Path | None = None
        sidecar_rollback: Path | None = None
        if self.latest_path.exists():
            archive_rollback = self._rollback_path("archive")
            os.link(self.latest_path, archive_rollback)
        if self.sidecar_path.exists():
            sidecar_rollback = self._rollback_path("sidecar")
            os.link(self.sidecar_path, sidecar_rollback)
        _fsync_directory(self.backup_directory)
        try:
            os.replace(partial, self.latest_path)
            os.replace(partial_sidecar, self.sidecar_path)
            _fsync_directory(self.backup_directory)
            self._verify_archive_pair(self.latest_path, self.sidecar_path)
            return archive_rollback, sidecar_rollback
        except Exception:
            self._restore_previous(archive_rollback, sidecar_rollback)
            raise

    def _restore_previous(
        self,
        archive_rollback: Path | None,
        sidecar_rollback: Path | None,
    ) -> None:
        if archive_rollback is None:
            self.latest_path.unlink(missing_ok=True)
        else:
            os.replace(archive_rollback, self.latest_path)
        if sidecar_rollback is None:
            self.sidecar_path.unlink(missing_ok=True)
        else:
            os.replace(sidecar_rollback, self.sidecar_path)
        _fsync_directory(self.backup_directory)

    def run(
        self,
        *,
        operation_id: str,
        started_at: str,
        set_stage: Callable[[str], None],
    ) -> dict[str, object]:
        """Build and atomically publish the single latest source backup."""

        started = time.monotonic()
        stage = "preflight"
        result: dict[str, object] = {
            "operation_id": operation_id,
            "started_at": started_at,
            "completed_stage": "admission",
        }
        partial = self.backup_directory / (
            f".source-backup-{uuid.uuid4().hex}.tar.gz.partial"
        )
        partial_sidecar = self.backup_directory / (
            f".source-backup-{uuid.uuid4().hex}.sha256.partial"
        )
        archive_rollback: Path | None = None
        sidecar_rollback: Path | None = None
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
            self._write_sidecar(partial, partial_sidecar)
            self._verify_archive_pair(partial, partial_sidecar)
            result["archive_bytes"] = int(partial.stat().st_size)
            result["completed_stage"] = "archive"

            stage = "publish"
            set_stage(stage)
            archive_rollback, sidecar_rollback = self._publish_archive(
                partial,
                partial_sidecar,
            )
            published = True
            result["completed_stage"] = "publish"
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )

            logger.info("source backup succeeded", extra={"result": result})
            published = False
            for rollback in (archive_rollback, sidecar_rollback):
                if rollback is not None:
                    rollback.unlink(missing_ok=True)
            _fsync_directory(self.backup_directory)
            return result
        except Exception as exc:
            partial.unlink(missing_ok=True)
            partial_sidecar.unlink(missing_ok=True)
            if published:
                try:
                    self._restore_previous(archive_rollback, sidecar_rollback)
                except Exception as rollback_exc:
                    exc = RuntimeError(
                        f"{exc}; cannot restore previous source backup: "
                        f"{rollback_exc}"
                    )
            else:
                for rollback in (archive_rollback, sidecar_rollback):
                    if rollback is not None:
                        rollback.unlink(missing_ok=True)
            result["finished_at"] = now_iso()
            result["duration_ms"] = int(
                round((time.monotonic() - started) * 1000)
            )
            result["failed_stage"] = stage
            result["error"] = str(exc)
            logger.exception("source backup failed", extra={"result": result})
            setattr(exc, "maintenance_result", result)
            raise

    def latest_archive_path(self) -> Path | None:
        """Resolve the latest regular archive without following a symlink."""

        candidate = self.latest_path
        sidecar = self.sidecar_path
        if (
            candidate.is_symlink()
            or not candidate.exists()
            or not candidate.is_file()
            or sidecar.is_symlink()
            or not sidecar.exists()
            or not sidecar.is_file()
        ):
            return None
        try:
            self._verify_archive_pair(candidate, sidecar)
        except (OSError, ValueError, tarfile.TarError, RuntimeError):
            logger.exception("published source backup failed verification")
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
