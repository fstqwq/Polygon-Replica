"""ZIP archive creation through the host compression process."""

import os
from pathlib import Path
import stat
import subprocess
import uuid


def _validate_archive_source(source_root: Path) -> Path:
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"archive source is not a directory: {source_root}")
    resolved_root = source_root.resolve(strict=True)
    has_entry = False
    for directory, directories, filenames in os.walk(
        source_root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        resolved_current = current.resolve(strict=True)
        if resolved_current != resolved_root and resolved_root not in resolved_current.parents:
            raise ValueError(f"archive source escaped its root: {current}")
        directories.sort()
        filenames.sort()
        for name in directories:
            child = current / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError(f"archive source contains a special file: {child}")
            resolved = child.resolve(strict=True)
            if resolved_root not in resolved.parents:
                raise ValueError(f"archive source escaped its root: {child}")
            has_entry = True
        for name in filenames:
            child = current / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"archive source contains a special file: {child}")
            resolved = child.resolve(strict=True)
            if resolved_root not in resolved.parents:
                raise ValueError(f"archive source escaped its root: {child}")
            has_entry = True
    if not has_entry:
        raise ValueError("archive source is empty")
    return resolved_root


def create_zip_archive(source_root: Path, target: Path) -> None:
    """Compress one validated directory as ZIP in a dedicated 7-Zip process."""

    resolved_root = _validate_archive_source(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve(strict=True)
    resolved_target = resolved_parent / target.name
    if resolved_target == resolved_root or resolved_root in resolved_target.parents:
        raise ValueError("archive output must be outside its source directory")

    temporary = resolved_parent / f".{target.name}.{uuid.uuid4().hex}.zip"
    try:
        try:
            result = subprocess.run(
                ["7z", "a", "-tzip", "-mx=1", "-bd", "-y", str(temporary), "."],
                cwd=resolved_root,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("7z executable is unavailable") from exc
        if result.returncode != 0:
            detail = result.stderr.strip()
            message = f"zip archive creation failed with exit code {result.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
