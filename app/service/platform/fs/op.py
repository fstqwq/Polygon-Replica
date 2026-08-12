import os
import shutil
import tarfile
import uuid
from pathlib import Path

from app.service.platform.git_process import run_git
from app.service.platform.workspace_path import is_hidden_workspace_path

def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _copytree_ignore(_src: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if is_hidden_workspace_path((name,)):
            ignored.add(name)
            continue
        if name == "__pycache__" or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=_copytree_ignore,
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
        proc = run_git(
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
            if member.issym() or member.islnk():
                raise RuntimeError(
                    f"source archive contains a symbolic link: {member.name}"
                )
            if not member.isfile():
                raise RuntimeError(
                    f"source archive contains a special file: {member.name}"
                )
            src = tf.extractfile(member)
            if src is None:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            out.chmod(member.mode & 0o777)

