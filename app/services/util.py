from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any


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

    stdin_fh = None
    stdout_fh = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "text": True,
            "timeout": timeout,
            "env": env,
            "check": False,
        }
        if input_text is not None:
            kwargs["input"] = input_text
        if stdin_path is not None:
            stdin_fh = stdin_path.open("r", encoding="utf-8", errors="replace")
            kwargs["stdin"] = stdin_fh

        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
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

    stdout_text = "" if stdout_path is not None else (proc.stdout or "")
    stderr_text = proc.stderr or ""
    return CmdResult(
        command=cmd,
        returncode=proc.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=int((monotonic() - start) * 1000),
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


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
    )
