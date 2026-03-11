from __future__ import annotations

import re

_LOG_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_LOG_BIDI_CONTROL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def compact_error_text(raw: str | None, *, max_chars: int = 240) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def sanitize_log_text_for_ui(raw: str, *, path_prefixes: list[str | tuple[str, str]] | None = None) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _LOG_ANSI_ESCAPE_RE.sub("", text)
    text = _LOG_CONTROL_CHAR_RE.sub("", text)
    text = _LOG_BIDI_CONTROL_RE.sub("", text)
    normalized = text.replace("\\", "/")
    pairs: list[tuple[str, str]] = []
    fallback_marker_index = 0
    for raw_item in path_prefixes or []:
        marker = ""
        prefix_raw = raw_item
        if isinstance(raw_item, tuple) and len(raw_item) >= 2:
            prefix_raw = raw_item[0]
            marker = str(raw_item[1] or "").strip().replace("\\", "/")
        prefix = str(prefix_raw or "").strip().replace("\\", "/")
        if not prefix:
            continue
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
    if truncated:
        if result:
            result = f"{result}\n..."
        else:
            result = "..."
    return result
