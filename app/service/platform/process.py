from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast

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
            stdout_text = cast(bytes, proc.stdout or b"").decode("utf-8", errors="replace")
        else:
            stdout_text = cast(str, proc.stdout or "")
    if binary_mode:
        stderr_text = cast(bytes, proc.stderr or b"").decode("utf-8", errors="replace")
    else:
        stderr_text = cast(str, proc.stderr or "")
    return CmdResult(
        command=cmd,
        returncode=proc.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=int((monotonic() - start) * 1000),
    )


def is_canonical_artifact_id(value: str) -> bool:
    return bool(ARTIFACT_ID_RE.fullmatch(str(value or "")))
