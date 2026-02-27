from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ExecSpec:
    command: list[str]
    cwd: Path | None = None
    timeout_sec: int = 30
    stdin_path: Path | None = None
    stdout_path: Path | None = None
    env: dict[str, str] | None = None
    memory_mb: int | None = None
    process_limit: int | None = None
    output_kb: int | None = None


@dataclass(frozen=True)
class ExecResult:
    backend: str
    status: str
    returncode: int | None
    elapsed_ms: int
    timed_out: bool = False
    memory_kb: int | None = None
    stdout: str = ""
    stderr: str = ""
    details: dict[str, object] = field(default_factory=dict)


class SandboxBackend:
    name: str = "unknown"

    def run(self, spec: ExecSpec) -> ExecResult:
        raise NotImplementedError

    def popen_command(self, spec: ExecSpec) -> list[str]:
        return [str(x) for x in spec.command]

    def popen(self, spec: ExecSpec, **kwargs) -> subprocess.Popen:
        cmd = self.popen_command(spec)
        if spec.cwd is not None and "cwd" not in kwargs:
            kwargs["cwd"] = spec.cwd
        if spec.env is not None and "env" not in kwargs:
            kwargs["env"] = spec.env
        return subprocess.Popen(cmd, **kwargs)
