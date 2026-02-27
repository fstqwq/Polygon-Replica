from __future__ import annotations

import fcntl
import os
import shutil
import threading
import time
from pathlib import Path

from app.db import DB
from app.services.util import is_canonical_artifact_id


class RuntimeCacheService:
    CLEANUP_LOCK = ".runtime-cache-cleanup.lock"

    def __init__(self, db: DB, artifacts_root: Path, run_root: Path):
        self.db = db
        self.artifacts_root = artifacts_root
        self.run_root = run_root
        self.cleanup_interval_sec = self._env_int(
            "POLYGONLIKE_ARTIFACT_CACHE_CLEANUP_INTERVAL_SEC",
            default=600,
            min_value=0,
            max_value=86400,
        )
        self.artifact_ttl_sec = self._env_int(
            "POLYGONLIKE_ARTIFACT_CACHE_TTL_SEC",
            default=604800,
            min_value=0,
            max_value=315360000,
        )
        self.run_ttl_sec = self._env_int(
            "POLYGONLIKE_RUN_CACHE_TTL_SEC",
            default=604800,
            min_value=0,
            max_value=315360000,
        )
        self._cleanup_state_lock = threading.Lock()
        self._last_cleanup_at = 0.0

    def _env_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def _acquire_file_lock(self, lock_path: Path, nonblocking: bool):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(lock_file.fileno(), mode)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    def _lock_path(self) -> Path:
        if self.artifacts_root.exists() or not self.run_root.exists():
            return self.artifacts_root / self.CLEANUP_LOCK
        return self.run_root / self.CLEANUP_LOCK

    def cleanup_cache(self, force: bool = False) -> bool:
        if self.artifact_ttl_sec <= 0 and self.run_ttl_sec <= 0:
            return False
        now_ts = time.time()
        with self._cleanup_state_lock:
            if not force and self.cleanup_interval_sec > 0:
                if (now_ts - self._last_cleanup_at) < float(self.cleanup_interval_sec):
                    return False
            self._last_cleanup_at = now_ts

        lock_path = self._lock_path()
        try:
            with self._acquire_file_lock(lock_path, nonblocking=True):
                self._cleanup_artifacts(now_ts)
                self._cleanup_runs(now_ts)
                return True
        except (BlockingIOError, OSError):
            return False

    def _safe_stat_mtime(self, path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    def _is_stale(self, path: Path, cutoff_ts: float) -> bool:
        return self._safe_stat_mtime(path) < cutoff_ts

    def _exported_build_ids(self) -> set[str]:
        rows = self.db.fetch_all("SELECT DISTINCT build_id FROM exports")
        out: set[str] = set()
        for row in rows:
            bid = str(row["build_id"] or "")
            if is_canonical_artifact_id(bid):
                out.add(bid)
        return out

    def _has_export_zip(self, artifact_dir: Path) -> bool:
        export_dir = artifact_dir / "export"
        if not export_dir.exists() or not export_dir.is_dir() or export_dir.is_symlink():
            return False
        try:
            with os.scandir(export_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if entry.name.endswith(".zip"):
                        return True
        except OSError:
            return False
        return False

    def _prune_to_export_only(self, artifact_dir: Path) -> None:
        export_dir = artifact_dir / "export"
        if not export_dir.exists() or not export_dir.is_dir() or export_dir.is_symlink():
            shutil.rmtree(artifact_dir, ignore_errors=True)
            return
        for child in artifact_dir.iterdir():
            if child.name == "export":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
                continue
            child.unlink(missing_ok=True)
        # Remove empty non-zip files under export to keep only deliverables.
        for child in export_dir.iterdir():
            try:
                is_file = child.is_file()
                is_symlink = child.is_symlink()
            except OSError:
                continue
            if not is_file or is_symlink:
                if child.is_dir() and not is_symlink:
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                continue
            if not child.name.endswith(".zip"):
                child.unlink(missing_ok=True)

    def _cleanup_artifacts(self, now_ts: float) -> None:
        if self.artifact_ttl_sec <= 0:
            return
        if not self.artifacts_root.exists() or not self.artifacts_root.is_dir():
            return
        cutoff = now_ts - float(self.artifact_ttl_sec)
        exported_builds = self._exported_build_ids()
        for problem_entry in self.artifacts_root.iterdir():
            if not problem_entry.is_dir() or problem_entry.is_symlink():
                continue
            for artifact_entry in list(problem_entry.iterdir()):
                if not artifact_entry.is_dir() or artifact_entry.is_symlink():
                    continue
                if not self._is_stale(artifact_entry, cutoff):
                    continue
                artifact_id = artifact_entry.name
                keep_exports = artifact_id in exported_builds or self._has_export_zip(artifact_entry)
                if keep_exports:
                    self._prune_to_export_only(artifact_entry)
                else:
                    shutil.rmtree(artifact_entry, ignore_errors=True)
            try:
                if not any(problem_entry.iterdir()):
                    problem_entry.rmdir()
            except OSError:
                pass

    def _cleanup_run_dir_children(self, root: Path, cutoff: float) -> None:
        if not root.exists() or not root.is_dir() or root.is_symlink():
            return
        for entry in list(root.iterdir()):
            if not self._is_stale(entry, cutoff):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    def _cleanup_runs(self, now_ts: float) -> None:
        if self.run_ttl_sec <= 0:
            return
        if not self.run_root.exists() or not self.run_root.is_dir():
            return
        cutoff = now_ts - float(self.run_ttl_sec)
        for entry in list(self.run_root.iterdir()):
            if entry.name == "invalid-runs" and entry.is_dir() and not entry.is_symlink():
                self._cleanup_run_dir_children(entry, cutoff)
                try:
                    if not any(entry.iterdir()):
                        entry.rmdir()
                except OSError:
                    pass
                continue
            if not self._is_stale(entry, cutoff):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
