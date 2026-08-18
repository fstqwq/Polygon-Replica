from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ExecSpec:
    command: list[str]
    cwd: Path | None = None
    read_only_mounts: tuple[Path, ...] = ()
    writable_mounts: tuple[Path, ...] = ()
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
