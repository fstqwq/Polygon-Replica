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
) -> CmdResult:
    start = monotonic()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
        env=env,
        check=False,
    )
    return CmdResult(
        command=cmd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
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
    shutil.copytree(src, dst)
