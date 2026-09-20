import app.main_constant as _K

from pathlib import Path

from app.service.platform.workspace_path import normalize_workspace_rel_path
from app.service.problem.solution_metadata import (
    EXPECTED_BEHAVIOR_VALUES,
    expected_behavior_label,
)



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
