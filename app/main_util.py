from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile

from app.runtime_value import RuntimeValues, build_runtime_values
from app.service.platform.workspace_path import (
    contains_symlink_component,
    normalize_workspace_rel_path,
    safe_workspace_path,
)


CPP_SOURCE_EXTENSIONS: set[str] = set()
SOLUTION_SOURCE_EXTENSIONS: set[str] = set()
GENERATOR_SOURCE_EXTENSIONS: set[str] = set()
TEXTAREA_MAX_BYTES = 256 * 1024
UPLOAD_MAX_BYTES = 256 * 1024 * 1024


def _apply_runtime_values(values: RuntimeValues) -> None:
    global CPP_SOURCE_EXTENSIONS
    global SOLUTION_SOURCE_EXTENSIONS
    global GENERATOR_SOURCE_EXTENSIONS
    global TEXTAREA_MAX_BYTES
    global UPLOAD_MAX_BYTES
    CPP_SOURCE_EXTENSIONS = {str(item).strip().lower() for item in values.CPP_SOURCE_EXTENSIONS}
    SOLUTION_SOURCE_EXTENSIONS = {
        str(item).strip().lower() for item in values.SOLUTION_SOURCE_EXTENSIONS
    }
    GENERATOR_SOURCE_EXTENSIONS = set(SOLUTION_SOURCE_EXTENSIONS)
    TEXTAREA_MAX_BYTES = int(values.TEXTAREA_MAX_BYTES)
    UPLOAD_MAX_BYTES = int(values.UPLOAD_MAX_BYTES)


def configure_runtime_values(values: RuntimeValues) -> None:
    _apply_runtime_values(values)


_apply_runtime_values(build_runtime_values())

_LOG_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_LOG_BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DOMJUDGE_INTERNAL_BUILD_PREFIX_RE = re.compile(
    r"/opt/domjudge/judgehost/judgings/[^:\s]+/endpoint-[^:\s]+/executable/[^:\s]+/[^:\s]+/build/"
)

def normalize_component_source_path(raw: str | None, folder: str, default_filename: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        normalized = f"{folder}/{default_filename}"
    expected_prefix = f"{folder}/"
    if not normalized.startswith(expected_prefix):
        raise ValueError(f"{folder} source must be under {folder}/")
    suffix = Path(normalized).suffix.lower()
    if folder == "solutions":
        allowed_exts = SOLUTION_SOURCE_EXTENSIONS
    elif folder == "generators":
        allowed_exts = GENERATOR_SOURCE_EXTENSIONS
    else:
        allowed_exts = CPP_SOURCE_EXTENSIONS
    if suffix not in allowed_exts:
        if folder in {"solutions", "generators"}:
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
    if folder == "solutions":
        allowed_exts = SOLUTION_SOURCE_EXTENSIONS
    elif folder == "generators":
        allowed_exts = GENERATOR_SOURCE_EXTENSIONS
    else:
        allowed_exts = CPP_SOURCE_EXTENSIONS
    if suffix not in allowed_exts:
        if folder in {"solutions", "generators"}:
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


def form_text(value: str | object) -> str:
    default = getattr(value, "default", value)
    if default is Ellipsis:
        return ""
    if default is None:
        return ""
    return str(default)


def enforce_textarea_max_bytes(
    value: str,
    *,
    label: str,
    max_bytes: int | None = None,
) -> str:
    safe_value = str(value)
    cap = TEXTAREA_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
    if len(safe_value.encode("utf-8")) > cap:
        raise ValueError(f"{label} is too long")
    return safe_value


async def read_upload_bytes_limited(
    upload: UploadFile,
    *,
    max_bytes: int | None = None,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    cap = UPLOAD_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError(f"{label} is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def write_upload_file_limited(
    upload: UploadFile,
    handle: BinaryIO,
    *,
    max_bytes: int | None = None,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> int:
    total = 0
    cap = UPLOAD_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError(f"{label} is too large")
        handle.write(chunk)
    return total


def read_fileobj_bytes_limited(
    fileobj: BinaryIO,
    *,
    max_bytes: int | None = None,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    cap = UPLOAD_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError(f"{label} is too large")
        chunks.append(chunk)
    return b"".join(chunks)


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



