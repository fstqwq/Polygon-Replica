from __future__ import annotations

import app.main_constant as _K

import os
from pathlib import Path

from app.impl.runtime.config import config
from app.main_util import normalize_workspace_rel_path, safe_workspace_path
from app.service.problem.solution_metadata import (
    EXPECTED_BEHAVIOR_VALUES,
    desc_rel_path_for_source,
    expected_behavior_label,
    infer_expected_behavior_from_name,
    render_solution_desc,
)

_C = config.config_values


def list_solution_sources(workspace: Path, limit: int = 64) -> tuple[list[str], bool]:
    base = workspace / "solutions"
    try:
        if not base.exists() or not base.is_dir() or base.is_symlink():
            return ([], False)
    except OSError:
        return ([], False)
    names: list[str] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = str(entry.name or "")
                if Path(name).suffix.lower() not in _K.SOLUTION_SOURCE_EXTENSIONS:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(f"solutions/{name}")
    except OSError:
        return ([], False)
    names.sort()
    truncated = len(names) > int(limit)
    if truncated:
        names = names[: int(limit)]
    return (names, truncated)


def solution_behavior_options() -> list[dict]:
    return [{"value": value, "label": expected_behavior_label(value)} for value in EXPECTED_BEHAVIOR_VALUES]


def normalize_solution_source_path_required(raw: str | None) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        raise ValueError("solution source is required")
    if not normalized.startswith("solutions/"):
        raise ValueError("solution source must be under solutions/")
    suffix = Path(normalized).suffix.lower()
    if suffix not in _K.SOLUTION_SOURCE_EXTENSIONS:
        raise ValueError("solution source must be .cpp/.cc/.cxx/.c++/.py/.java")
    return normalized


def ensure_solution_metadata_for_source(workspace: Path, source_rel: str) -> bool:
    source = normalize_solution_source_path_required(source_rel)
    expected = infer_expected_behavior_from_name(source)
    desc_rel = desc_rel_path_for_source(source)
    desc_abs = safe_workspace_path(workspace, desc_rel)
    if desc_abs.exists() and desc_abs.is_file() and (desc_abs.stat().st_size > 0):
        return False
    config.git_service.write_file(workspace, desc_rel, render_solution_desc(expected, ""))
    return True

