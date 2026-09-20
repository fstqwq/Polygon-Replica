import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic


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
    normalized_args = args[1:] if args and args[0] == "git" else args
    command = ["git", *normalized_args]
    if stdout_path is None:
        proc = subprocess.run(
            command, cwd=cwd, timeout=timeout, check=False,
            text=True, capture_output=True,
        )
        returncode = proc.returncode
        stdout_text = proc.stdout
        stderr_text = proc.stderr
    else:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("wb") as stdout_fh:
            binary_proc = subprocess.run(
                command, cwd=cwd, timeout=timeout, check=False,
                stdout=stdout_fh, stderr=subprocess.PIPE,
            )
        returncode = binary_proc.returncode
        stdout_text = ""
        stderr_text = binary_proc.stderr.decode("utf-8", errors="replace")
    return GitCommandResult(
        args=command,
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        elapsed_ms=int((monotonic() - start) * 1000),
    )
