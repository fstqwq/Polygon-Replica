from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.runtime_value import RuntimeValues, build_runtime_values

CPP_SOURCE_EXTENSIONS: set[str] = set()
SOLUTION_SOURCE_EXTENSIONS: set[str] = set()


def apply_runtime_values(values: RuntimeValues) -> None:
    global CPP_SOURCE_EXTENSIONS
    global SOLUTION_SOURCE_EXTENSIONS
    CPP_SOURCE_EXTENSIONS = {str(item).strip().lower() for item in values.CPP_SOURCE_EXTENSIONS}
    SOLUTION_SOURCE_EXTENSIONS = {
        str(item).strip().lower() for item in values.SOLUTION_SOURCE_EXTENSIONS
    }


apply_runtime_values(build_runtime_values())


def contains_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        if root.is_symlink():
            return True
    except OSError:
        return True
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return True
    cur = root
    for part in rel.parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True
        if not cur.exists():
            break
    return False


def safe_workspace_path(workspace: Path, rel: str, allow_workspace_root: bool = False) -> Path:
    ws_root = workspace.resolve()
    candidate = workspace / rel
    path = candidate.resolve()
    if ws_root not in path.parents and ws_root != path:
        raise HTTPException(status_code=400, detail="invalid path")
    if contains_symlink_component(ws_root, candidate):
        raise HTTPException(status_code=400, detail="invalid path")
    if not allow_workspace_root and path == ws_root:
        raise HTTPException(status_code=400, detail="invalid path")
    try:
        rel_parts = path.relative_to(ws_root).parts
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    if ".git" in rel_parts or ".polygonlike.lock" in rel_parts:
        raise HTTPException(status_code=400, detail="reserved path")
    return path


def normalize_workspace_rel_path(raw: str | None) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value or value.startswith("/"):
        return ""
    parts: list[str] = []
    for part in value.split("/"):
        item = part.strip()
        if not item or item == ".":
            continue
        if item == "..":
            return ""
        parts.append(item)
    if not parts:
        return ""
    return "/".join(parts)


def normalize_component_source_path(raw: str | None, folder: str, default_filename: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        normalized = f"{folder}/{default_filename}"
    expected_prefix = f"{folder}/"
    if not normalized.startswith(expected_prefix):
        raise ValueError(f"{folder} source must be under {folder}/")
    suffix = Path(normalized).suffix.lower()
    allowed_exts = SOLUTION_SOURCE_EXTENSIONS if folder == "solutions" else CPP_SOURCE_EXTENSIONS
    if suffix not in allowed_exts:
        if folder == "solutions":
            raise ValueError(f"{folder} source must be .cpp/.cc/.cxx/.c++/.py/.java")
        raise ValueError(f"{folder} source must be a C++ file")
    return normalized


def normalize_optional_component_source_path(raw: str | None, folder: str, label: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        return ""
    expected_prefix = f"{folder}/"
    if not normalized.startswith(expected_prefix):
        raise ValueError(f"{label} must be under {folder}/")
    suffix = Path(normalized).suffix.lower()
    allowed_exts = SOLUTION_SOURCE_EXTENSIONS if folder == "solutions" else CPP_SOURCE_EXTENSIONS
    if suffix not in allowed_exts:
        if folder == "solutions":
            raise ValueError(f"{label} must be .cpp/.cc/.cxx/.c++/.py/.java")
        raise ValueError(f"{label} must be a C++ file")
    return normalized


def normalize_optional_component_source_path_safe(raw: str | None, folder: str, label: str) -> str:
    try:
        return normalize_optional_component_source_path(raw, folder, label)
    except ValueError:
        return ""
