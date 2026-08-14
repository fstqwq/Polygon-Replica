"""Shared request/file normalization helpers."""

import re
import sqlite3
from pathlib import Path
from typing import BinaryIO

from starlette.datastructures import UploadFile

from app.main_constant import CPP_SOURCE_EXTENSIONS, SOLUTION_SOURCE_EXTENSIONS
from app.service.platform.workspace_path import normalize_workspace_rel_path


GENERATOR_SOURCE_EXTENSIONS = SOLUTION_SOURCE_EXTENSIONS

_LOG_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_LOG_BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DOMJUDGE_INTERNAL_BUILD_PREFIX_RE = re.compile(
    r"/opt/domjudge/judgehost/judgings/[^:\s]+/endpoint-[^:\s]+/executable/[^:\s]+/[^:\s]+/build/"
)
SQL_TRACE_JSON_FIELDS = ("details_json", "value_json")


def coerce_bool(value: object, default: bool = False) -> bool:
    """Parse a permissive external boolean value into a canonical bool."""

    if value is True:
        return True
    if value is False:
        return False
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def trace_sql_verb(text: str) -> str:
    """Return the leading SQL verb for compact trace diagnostics."""

    match = re.match(r"^\s*([A-Za-z]+)", str(text or ""))
    return str(match.group(1) if match else "SQL").upper()


def trace_sql_table(text: str) -> str:
    """Best-effort table extraction for SQL trace diagnostics."""

    raw = str(text or "")
    patterns = (
        r"^\s*UPDATE\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        r"^\s*DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        r"^\s*SELECT\b.*?\bFROM\s+([A-Za-z_][A-Za-z0-9_\.]*)",
        r"^\s*PRAGMA\s+([A-Za-z_][A-Za-z0-9_\.]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match is not None:
            return str(match.group(1) or "").strip()
    return ""


def truncate_trace_text(text: str, *, limit: int) -> str:
    """Truncate trace text while preserving its original length in the marker."""

    safe_text = str(text or "").strip()
    cap = max(64, int(limit))
    if len(safe_text) <= cap:
        return safe_text
    return f"{safe_text[:cap].rstrip()}... [truncated; len={len(safe_text)}]"


def summarize_traced_sql(statement: str, *, text_limit: int) -> str:
    """Summarize SQL trace output without dumping JSON payload columns."""

    text = " ".join(str(statement or "").strip().split())
    if not text:
        return ""
    lowered = text.lower()
    verb = trace_sql_verb(text)
    table = trace_sql_table(text)
    json_fields = [field for field in SQL_TRACE_JSON_FIELDS if field in lowered]
    if not json_fields:
        return truncate_trace_text(text, limit=text_limit)
    field_positions = [
        lowered.find(field) for field in json_fields if lowered.find(field) >= 0
    ]
    prefix_end = min(field_positions) if field_positions else len(text)
    prefix = text[:prefix_end].rstrip(" ,")
    if not prefix:
        prefix = f"{verb} {table}".strip()
    prefix = truncate_trace_text(prefix, limit=max(96, text_limit // 2))
    table_part = table or "?"
    fields_part = ",".join(json_fields)
    return (
        f"{verb} {table_part} [json_fields={fields_part} len={len(text)}] "
        f"{prefix} <redacted-json>"
    )


def is_sqlite_locked_error(exc: sqlite3.OperationalError) -> bool:
    """Return whether a sqlite operational error is a lock contention error."""

    msg = str(exc or "").strip().lower()
    if not msg:
        return False
    return "database is locked" in msg or "database table is locked" in msg

def normalize_component_source_path(raw: str | None, folder: str, default_filename: str) -> str:
    """Normalize a required component source path under its component folder."""

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
    """Normalize an optional component source path under its component folder."""

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
    """Normalize an optional component source path, returning empty on invalid input."""

    try:
        return normalize_optional_component_source_path(raw, folder, label)
    except ValueError:
        return ""


def form_text(value: str | object) -> str:
    """Convert form defaults and values to plain text."""

    default = getattr(value, "default", value)
    if default is Ellipsis:
        return ""
    if default is None:
        return ""
    return str(default)


def problem_slug_leaf(value: str | object) -> str:
    """Return the final path segment from a problem slug-like value."""

    raw = form_text(value).strip().replace("\\", "/")
    if not raw:
        return ""
    parts = [segment for segment in raw.split("/") if segment]
    if not parts:
        return ""
    return parts[-1]


def normalize_form_text_newlines(value: str | object) -> str:
    """Normalize CRLF/CR form text to LF."""

    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def enforce_textarea_max_bytes(
    value: str,
    *,
    label: str,
    max_bytes: int,
) -> str:
    """Validate textarea payload size after newline normalization."""

    safe_value = normalize_form_text_newlines(value)
    cap = max(1, int(max_bytes))
    if len(safe_value.encode("utf-8")) > cap:
        raise ValueError(f"{label} is too long")
    return safe_value


async def read_upload_bytes_limited(
    upload: UploadFile,
    *,
    max_bytes: int,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read an UploadFile into memory with a byte cap."""

    chunks: list[bytes] = []
    total = 0
    cap = max(1, int(max_bytes))
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
    max_bytes: int,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> int:
    """Stream an UploadFile to a binary handle with a byte cap."""

    total = 0
    cap = max(1, int(max_bytes))
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
    max_bytes: int,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read a binary file-like object into memory with a byte cap."""

    chunks: list[bytes] = []
    total = 0
    cap = max(1, int(max_bytes))
    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError(f"{label} is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def write_fileobj_limited(
    fileobj: BinaryIO,
    handle: BinaryIO,
    *,
    max_bytes: int,
    label: str = "uploaded file",
    chunk_size: int = 1024 * 1024,
) -> int:
    """Stream a synchronous uploaded file to disk with a compressed-byte cap."""

    total = 0
    cap = max(1, int(max_bytes))
    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ValueError(f"{label} is too large")
        handle.write(chunk)
    return total


def sanitize_log_text_for_ui(
    raw: str,
    *,
    path_prefixes: list[tuple[str, str]] | None = None,
) -> str:
    """Remove unsafe control characters and redact known path prefixes from logs."""

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
