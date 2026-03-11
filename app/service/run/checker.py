from __future__ import annotations

import os
from pathlib import Path

from app.service.sandbox.base import ExecSpec, SandboxBackend

from .runtime import DIAG_RE


def collect_diagnostics(workspace: Path | None, text: str) -> list[dict]:
    result: list[dict] = []
    workspace_resolved: Path | None = None
    if workspace is not None:
        try:
            workspace_resolved = workspace.resolve()
        except OSError:
            workspace_resolved = None
    for line in text.splitlines():
        m = DIAG_RE.match(line.strip())
        if not m:
            continue
        file_path = Path(m.group("file"))
        rel = str(file_path)
        can_link = False
        if workspace_resolved is not None:
            try:
                if file_path.is_absolute():
                    resolved = file_path.resolve()
                else:
                    resolved = (workspace_resolved / file_path).resolve()
                rel = str(resolved.relative_to(workspace_resolved))
                can_link = True
            except Exception:
                rel = str(file_path)
                can_link = False
        result.append(
            {
                "file": rel,
                "line": int(m.group("line")),
                "column": int(m.group("col")),
                "level": m.group("level"),
                "message": m.group("msg"),
                "can_link": can_link,
            }
        )
    return result


def validator_style_verdict(rc: int) -> str:
    code = int(rc)
    if code == 42:
        return "OK"
    if code == 43:
        return "WA"
    return "FL"


def run_checker(
    sandbox: SandboxBackend,
    checker: Path,
    *,
    checker_args: list[str],
    test: Path,
    team_output: Path,
    answer: Path,
    pass_feedback_dir: Path,
    run_timeout_sec: int,
    run_memory_mb: int,
    run_process_limit: int,
    run_output_kb: int,
) -> tuple[object, str, bool]:
    env = dict(os.environ)
    env["FEEDBACK_DIR"] = str(pass_feedback_dir)
    feedback_arg = str(pass_feedback_dir) + os.sep
    check_result = sandbox.run(
        ExecSpec(
            command=[str(checker), str(test), str(answer), feedback_arg, *checker_args],
            stdin_path=team_output,
            timeout_sec=run_timeout_sec,
            cwd=pass_feedback_dir,
            env=env,
            memory_mb=run_memory_mb,
            process_limit=run_process_limit,
            output_kb=run_output_kb,
        )
    )
    checker_log = (check_result.stdout or "") + (check_result.stderr or "")
    checker_timed_out = bool(check_result.timed_out)
    return check_result, checker_log, checker_timed_out


