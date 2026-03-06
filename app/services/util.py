from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class CmdResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 120,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
) -> CmdResult:
    start = monotonic()
    if input_text is not None and stdin_path is not None:
        raise ValueError("input_text and stdin_path are mutually exclusive")
    binary_mode = input_text is None and (stdin_path is not None or stdout_path is not None)

    stdin_fh = None
    stdout_fh = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "text": not binary_mode,
            "timeout": timeout,
            "env": env,
            "check": False,
        }
        if input_text is not None:
            kwargs["input"] = input_text
        if stdin_path is not None:
            if binary_mode:
                stdin_fh = stdin_path.open("rb")
            else:
                stdin_fh = stdin_path.open("r", encoding="utf-8", errors="replace")
            kwargs["stdin"] = stdin_fh

        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            if binary_mode:
                stdout_fh = stdout_path.open("wb")
            else:
                stdout_fh = stdout_path.open("w", encoding="utf-8")
            kwargs["stdout"] = stdout_fh
            kwargs["stderr"] = subprocess.PIPE
        else:
            kwargs["capture_output"] = True

        proc = subprocess.run(cmd, **kwargs)
    finally:
        if stdin_fh is not None:
            stdin_fh.close()
        if stdout_fh is not None:
            stdout_fh.close()

    if stdout_path is not None:
        stdout_text = ""
    else:
        if binary_mode:
            stdout_raw = proc.stdout or b""
        else:
            stdout_raw = proc.stdout or ""
        if isinstance(stdout_raw, bytes):
            stdout_text = stdout_raw.decode("utf-8", errors="replace")
        else:
            stdout_text = stdout_raw
    if binary_mode:
        stderr_raw = proc.stderr or b""
    else:
        stderr_raw = proc.stderr or ""
    if isinstance(stderr_raw, bytes):
        stderr_text = stderr_raw.decode("utf-8", errors="replace")
    else:
        stderr_text = stderr_raw
    return CmdResult(
        command=cmd,
        returncode=proc.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=int((monotonic() - start) * 1000),
    )


def is_canonical_artifact_id(value: str) -> bool:
    return bool(ARTIFACT_ID_RE.fullmatch(str(value or "")))


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
            # Keep snapshot materialized as plain files/directories only.
            if member.issym() or member.islnk() or not member.isfile():
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
