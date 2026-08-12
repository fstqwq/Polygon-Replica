"""Safe filesystem boundary for authored problem-source files."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
    is_repository_answer_path,
)


def _validate_authored_relative(relative: str) -> None:
    parts = PurePosixPath(relative).parts
    if is_hidden_workspace_path(parts):
        raise ValueError(f"{relative}: hidden source paths are not allowed")
    if not is_allowed_workspace_root_path(parts):
        raise ValueError(f"{relative}: problem source root is not allowed")
    if is_repository_answer_path(parts):
        raise ValueError(f"{relative}: materialized answers are not authored source")


def validate_source_tree_filesystem(root: Path) -> None:
    """Reject links and special files anywhere in an authored source tree."""

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        if root.is_symlink():
            raise ValueError("problem source root must not be a symbolic link")
        if not root.is_dir():
            raise ValueError("problem source root must be a directory")
        resolved_root = root.resolve(strict=True)
        for dirpath, dirnames, filenames in os.walk(
            resolved_root,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            parent = Path(dirpath)
            if parent == resolved_root:
                dirnames[:] = [
                    name
                    for name in sorted(dirnames)
                    if name not in {".git", "test_data"}
                ]
            else:
                dirnames[:] = sorted(dirnames)
            for name in dirnames:
                directory = parent / name
                relative = directory.relative_to(resolved_root).as_posix()
                if directory.is_symlink():
                    raise ValueError(
                        f"{relative}: symbolic links are not allowed"
                    )
                if not directory.is_dir():
                    raise ValueError(f"{relative}: special files are not allowed")
                _validate_authored_relative(relative)
            for name in sorted(filenames):
                if parent == resolved_root and name in {".git", "test_data"}:
                    continue
                path = parent / name
                relative = path.relative_to(resolved_root).as_posix()
                if path.is_symlink():
                    raise ValueError(
                        f"{relative}: symbolic links are not allowed"
                    )
                if not path.is_file():
                    raise ValueError(f"{relative}: special files are not allowed")
                _validate_authored_relative(relative)
    except OSError as exc:
        raise ValueError(f"cannot inspect problem source tree: {exc}") from exc


def require_regular_source_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    path_parts = pure.parts
    if (
        not relative
        or "\x00" in relative
        or "\\" in relative
        or pure.is_absolute()
        or pure.as_posix() != relative
        or not path_parts
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        raise ValueError(f"{relative}: invalid problem source path")
    try:
        if root.is_symlink():
            raise ValueError("problem source root must not be a symbolic link")
        root_resolved = root.resolve(strict=True)
        path = root
        for part in path_parts:
            path /= part
            if path.is_symlink():
                raise ValueError(f"{relative}: symbolic links are not allowed")
        if not path.is_file():
            raise ValueError(f"{relative}: required regular file is missing")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{relative}: cannot inspect file: {exc}") from exc
    if root_resolved not in resolved.parents:
        raise ValueError(f"{relative}: path escapes the problem source root")
    return path
