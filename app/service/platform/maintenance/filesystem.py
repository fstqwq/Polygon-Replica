"""Filesystem safety, measurement, and deletion mechanics for maintenance."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.service.platform.fs.layout import StorageLayout
from app.service.platform.maintenance.plan import CleanupFilesystemClass


def assert_cleanup_tree_safe(root: Path) -> None:
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
    """Reject unsafe roots before startup-cleared cache state is touched."""

    roots = storage_layout.validate()
    assert_cleanup_tree_safe(roots["cache_root"])
    return roots


class ArtifactCleanupFilesystem:
    def __init__(self, storage_layout: StorageLayout) -> None:
        self._storage = storage_layout

    def roots(
        self,
        classes: tuple[CleanupFilesystemClass, ...],
    ) -> dict[CleanupFilesystemClass, Path]:
        available: dict[CleanupFilesystemClass, Path] = {
            "artifacts_root": self._storage.artifacts_root.absolute(),
            "cache_root": self._storage.cache_root.absolute(),
        }
        return {storage_class: available[storage_class] for storage_class in classes}

    def preflight(
        self,
        classes: tuple[CleanupFilesystemClass, ...],
        *,
        create_roots: bool = False,
    ) -> dict[str, object]:
        roots = validate_runtime_startup_preconditions(self._storage)
        cleanup_roots = self.roots(classes)
        for root in cleanup_roots.values():
            assert_cleanup_tree_safe(root)
        if create_roots:
            for root in cleanup_roots.values():
                root.mkdir(parents=True, exist_ok=True)
        result: dict[str, object] = {
            name: str(root) for name, root in roots.items()
        }
        result["database"] = str(self._storage.database_path.absolute().resolve())
        return result

    @staticmethod
    def tree_usage(root: Path) -> tuple[int, int]:
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
    def tree_bytes(cls, root: Path) -> int:
        total, _files = cls.tree_usage(root)
        return total

    @classmethod
    def clear_root(cls, root: Path) -> int:
        if root.is_symlink():
            raise RuntimeError(f"cleanup root must not be a symlink: {root}")
        root.mkdir(parents=True, exist_ok=True)
        assert_cleanup_tree_safe(root)
        before = cls.tree_bytes(root)
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        root.mkdir(parents=True, exist_ok=True)
        return max(0, before - cls.tree_bytes(root))
