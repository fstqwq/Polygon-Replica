from __future__ import annotations

import json
import os
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Any

from app.service.platform.process import run_cmd


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(".git", ".polygonlike.lock", "__pycache__", "*.pyc"),
        symlinks=True,
    )


def remove_symlinks(root: Path) -> int:
    removed = 0
    if not root.exists() or not root.is_dir():
        return removed
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dir_root = Path(dirpath)
        keep_dirs: list[str] = []
        for name in dirnames:
            p = dir_root / name
            if p.is_symlink():
                p.unlink(missing_ok=True)
                removed += 1
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in filenames:
            p = dir_root / name
            if p.is_symlink():
                p.unlink(missing_ok=True)
                removed += 1
    return removed


def extract_git_archive(workspace: Path, commit: str, target: Path, timeout: int = 120) -> None:
    target.mkdir(parents=True, exist_ok=True)
    tmp_tar = target.parent / f".archive-{uuid.uuid4().hex[:12]}.tar"
    try:
        proc = run_cmd(
            [
                "git",
                "-C",
                str(workspace),
                "archive",
                "--format=tar",
                "-o",
                str(tmp_tar),
                commit,
            ],
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout)
        _extract_tar_safe(tmp_tar, target)
    finally:
        tmp_tar.unlink(missing_ok=True)


def _extract_tar_safe(tar_path: Path, target: Path) -> None:
    target_root = target.resolve()
    with tarfile.open(tar_path, "r:") as tf:
        for member in tf.getmembers():
            rel = Path(member.name)
            if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                raise RuntimeError(f"unsafe archive entry: {member.name}")
            out = (target_root / rel).resolve()
            if target_root not in out.parents and out != target_root:
                raise RuntimeError(f"archive entry escapes target: {member.name}")
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            # Keep snapshot built as plain files/directories only.
            if member.issym() or member.islnk() or not member.isfile():
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)



