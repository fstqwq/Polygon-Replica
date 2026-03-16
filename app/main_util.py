from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException

from app.runtime_value import RuntimeValues, build_runtime_values


CPP_SOURCE_EXTENSIONS: set[str] = set()
SOLUTION_SOURCE_EXTENSIONS: set[str] = set()


def _apply_runtime_values(values: RuntimeValues) -> None:
    global CPP_SOURCE_EXTENSIONS
    global SOLUTION_SOURCE_EXTENSIONS
    CPP_SOURCE_EXTENSIONS = {str(item).strip().lower() for item in values.CPP_SOURCE_EXTENSIONS}
    SOLUTION_SOURCE_EXTENSIONS = {
        str(item).strip().lower() for item in values.SOLUTION_SOURCE_EXTENSIONS
    }


def configure_runtime_values(values: RuntimeValues) -> None:
    _apply_runtime_values(values)


_apply_runtime_values(build_runtime_values())

_LOG_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_LOG_BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DOMJUDGE_INTERNAL_BUILD_PREFIX_RE = re.compile(
    r"/opt/domjudge/judgehost/judgings/[^:\s]+/endpoint-[^:\s]+/executable/[^:\s]+/[^:\s]+/build/"
)


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


def compact_error_text(raw: str | None, *, max_chars: int = 240) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def preserve_error_text(raw: str | None, *, max_chars: int = 1600, max_lines: int = 20) -> str:
    text = sanitize_log_text_for_ui(str(raw or ""))
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines = [str(line or "").rstrip() for line in lines]
    while normalized_lines and (not normalized_lines[0].strip()):
        normalized_lines.pop(0)
    while normalized_lines and (not normalized_lines[-1].strip()):
        normalized_lines.pop()
    if not normalized_lines:
        return ""
    folded: list[str] = []
    prev_blank = False
    for line in normalized_lines:
        if line.strip():
            folded.append(line)
            prev_blank = False
            continue
        if prev_blank:
            continue
        folded.append("")
        prev_blank = True
    truncated = False
    line_cap = max(1, int(max_lines))
    if len(folded) > line_cap:
        folded = folded[:line_cap]
        truncated = True
    result = "\n".join(folded).strip()
    char_cap = max(1, int(max_chars))
    if len(result) > char_cap:
        result = result[:char_cap].rstrip()
        truncated = True
    if truncated and result:
        if not result.endswith("..."):
            result = f"{result}\n..."
    return result


def sanitize_log_text_for_ui(raw: str, *, path_prefixes: list[tuple[str, str]] | None = None) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _LOG_ANSI_ESCAPE_RE.sub("", text)
    text = _LOG_CONTROL_CHAR_RE.sub("", text)
    text = _LOG_BIDI_CONTROL_RE.sub("", text)
    normalized = text.replace("\\", "/")
    normalized = _DOMJUDGE_INTERNAL_BUILD_PREFIX_RE.sub("", normalized)
    pairs: list[tuple[str, str]] = []
    fallback_marker_index = 0
    for prefix_raw, marker_raw in path_prefixes or []:
        prefix = str(prefix_raw or "").strip().replace("\\", "/")
        if not prefix:
            continue
        marker = str(marker_raw or "").strip().replace("\\", "/")
        if not marker:
            fallback_marker_index += 1
            marker = f"__redacted_path_{fallback_marker_index}__"
        prefix_token = prefix.rstrip("/") + "/"
        marker_token = marker.rstrip("/") + "/"
        pairs.append((prefix_token, marker_token))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    for prefix_token, marker_token in pairs:
        normalized = normalized.replace(prefix_token, marker_token)
    return normalized



