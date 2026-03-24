from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int


def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    stdout_path: Path | None = None,
) -> GitCommandResult:
    start = monotonic()
    stdout_fh = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "text": stdout_path is None,
            "timeout": timeout,
            "check": False,
        }
        normalized_args = list(args)
        if normalized_args and normalized_args[0] == "git":
            normalized_args = normalized_args[1:]
        command = ["git", *normalized_args]
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_fh = stdout_path.open("wb")
            kwargs["stdout"] = stdout_fh
            kwargs["stderr"] = subprocess.PIPE
        else:
            kwargs["capture_output"] = True
        proc = subprocess.run(command, **kwargs)
    finally:
        if stdout_fh is not None:
            stdout_fh.close()

    if stdout_path is None:
        stdout_text = cast(str, proc.stdout or "")
    else:
        stdout_text = ""
    if stdout_path is None:
        stderr_text = cast(str, proc.stderr or "")
    else:
        stderr_text = cast(bytes, proc.stderr or b"").decode("utf-8", errors="replace")
    return GitCommandResult(
        args=command,
        returncode=int(proc.returncode),
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=int((monotonic() - start) * 1000),
    )
