from __future__ import annotations

import re

from app.main_constant import AUX_DISPLAY_TEXT_LIMIT_BYTES

_LOG_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_LOG_BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DOMJUDGE_INTERNAL_BUILD_PREFIX_RE = re.compile(
    r"/opt/domjudge/judgehost/judgings/[^:\s]+/endpoint-[^:\s]+/executable/[^:\s]+/[^:\s]+/build/"
)
_TRUNCATION_MARKER = "..."


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


def aux_display_text_limit_bytes(constants: object | None = None) -> int:
    default = max(1, int(AUX_DISPLAY_TEXT_LIMIT_BYTES))
    source = constants
    if source is None:
        try:
            from app.impl.runtime.config import config

            source = getattr(config, "constants", None)
        except Exception:
            source = None
    if source is None:
        return default
    try:
        return max(1, int(getattr(source, "AUX_DISPLAY_TEXT_LIMIT_BYTES", default) or default))
    except Exception:
        return default


def normalize_display_text(raw: str | None, *, path_prefixes: list[tuple[str, str]] | None = None) -> str:
    text = sanitize_log_text_for_ui(str(raw or ""), path_prefixes=path_prefixes)
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
    return "\n".join(normalized_lines)


def truncate_utf8_text_bytes(
    text: str,
    *,
    max_bytes: int,
    marker: str = _TRUNCATION_MARKER,
) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    value = str(text or "")
    raw = value.encode("utf-8")
    if len(raw) <= cap:
        return (value, False)
    marker_raw = str(marker or "").encode("utf-8")
    if len(marker_raw) >= cap:
        return (marker_raw[:cap].decode("utf-8", errors="ignore"), True)
    keep = max(0, cap - len(marker_raw))
    prefix = raw[:keep].decode("utf-8", errors="ignore").rstrip()
    if not prefix:
        return (marker_raw[:cap].decode("utf-8", errors="ignore"), True)
    return (prefix + marker, True)


def truncate_display_text(
    raw: str | None,
    *,
    limit_bytes: int | None = None,
    path_prefixes: list[tuple[str, str]] | None = None,
) -> tuple[str, bool]:
    normalized = normalize_display_text(raw, path_prefixes=path_prefixes)
    if not normalized:
        return ("", False)
    limit = aux_display_text_limit_bytes() if limit_bytes is None else max(1, int(limit_bytes))
    return truncate_utf8_text_bytes(normalized, max_bytes=limit)


def bounded_display_text(
    raw: str | None,
    *,
    limit_bytes: int | None = None,
    path_prefixes: list[tuple[str, str]] | None = None,
) -> str:
    text, _truncated = truncate_display_text(
        raw,
        limit_bytes=limit_bytes,
        path_prefixes=path_prefixes,
    )
    return text
