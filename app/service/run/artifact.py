from __future__ import annotations

from pathlib import Path
from typing import Callable


def persist_compile_outputs(
    compile_log_file: Path,
    workspace: Path | None,
    stdout_text: str,
    stderr_text: str,
    *,
    collect_diagnostics: Callable[[Path | None, str], list[dict]],
) -> list[dict]:
    diagnostics: list[dict] = []
    wrote_any = False
    saw_stream_text = False
    with compile_log_file.open("w", encoding="utf-8") as clog:
        for chunk in (stdout_text, stderr_text):
            text = str(chunk or "")
            if not text:
                continue
            saw_stream_text = True
            if wrote_any and not text.startswith("\n"):
                clog.write("\n")
            clog.write(text)
            if not text.endswith("\n"):
                clog.write("\n")
            diagnostics.extend(collect_diagnostics(workspace, text))
            wrote_any = True
    if not saw_stream_text:
        diagnostics.extend(collect_diagnostics(workspace, ""))
    return diagnostics


def resolve_submission_source(
    workspace: Path,
    submission_path: str,
    *,
    contains_symlink_component: Callable[[Path, Path], bool],
) -> Path:
    candidate = workspace / submission_path
    ws_resolved = workspace.resolve()
    source = candidate.resolve()
    if ws_resolved not in source.parents:
        raise RuntimeError("submission_path must be inside the workspace")
    if contains_symlink_component(ws_resolved, candidate):
        raise RuntimeError("submission_path cannot include symlink path components")
    try:
        rel_parts = source.relative_to(ws_resolved).parts
    except ValueError as exc:
        raise RuntimeError("submission_path must be inside the workspace") from exc
    if ".git" in rel_parts or ".polygonlike.lock" in rel_parts:
        raise RuntimeError("submission_path is reserved")
    if not source.exists() or not source.is_file():
        raise RuntimeError(f"submission source not found: {submission_path}")
    return source


