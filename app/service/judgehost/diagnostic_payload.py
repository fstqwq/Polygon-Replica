import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.service.judgehost.shared import domjudge_lower_text, domjudge_text


@dataclass(frozen=True, slots=True)
class ParsedDiagnosticPayload:
    """Canonical text selected from one bounded Judgehost diagnostic payload."""

    text: str


def _decode_maybe_base64(text: str) -> str:
    if not text:
        return ""
    compact = "".join(text.split())
    if (
        compact
        and len(compact) % 4 == 0
        and re.fullmatch(r"[A-Za-z0-9+/=]+", compact)
    ):
        try:
            blob = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError, TypeError):
            blob = b""
        if blob:
            decoded = blob.decode("utf-8", errors="replace").strip()
            if decoded:
                printable = sum(
                    char.isprintable() or char in {"\n", "\r", "\t"}
                    for char in decoded
                )
                if printable >= int(len(decoded) * 0.9):
                    return decoded
    return text


def _looks_like_raw_base64(text: str) -> bool:
    compact = "".join(text.split())
    return bool(
        compact
        and len(compact) >= 64
        and len(compact) % 4 == 0
        and re.fullmatch(r"[A-Za-z0-9+/=]+", compact)
    )


def parse_diagnostic_payload(
    payload: Mapping[str, object],
) -> ParsedDiagnosticPayload:
    """Select bounded, useful text without retaining opaque uploaded blobs."""

    if not payload:
        return ParsedDiagnosticPayload(text="")
    interesting_markers = (
        "fail",
        "error",
        "exception",
        "trace",
        "crash",
        "compare",
        "expected",
        "unexpected",
    )
    handled_keys = {
        "judgehostlog",
        "description",
        "message",
        "error",
        "detail",
        "details",
        "stderr",
        "stdout",
        "output_error",
        "output_system",
        "output_diff",
        "compare_output",
        "compare_error",
        "judgemessage",
        "team_message",
    }
    lines: list[str] = []
    seen: set[str] = set()

    def append_text(text: str) -> None:
        decoded = _decode_maybe_base64(text)
        if not decoded:
            return
        normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
        for raw_line in normalized.split("\n"):
            line = domjudge_text(raw_line)
            if not line:
                continue
            token = line.lower()
            if token in seen:
                continue
            seen.add(token)
            lines.append(line)
            if len(lines) >= 16:
                return

    def append_judgehost_log(text: str) -> None:
        decoded = _decode_maybe_base64(text)
        if not decoded:
            return
        normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = [domjudge_text(item) for item in normalized.split("\n")]
        raw_lines = [item for item in raw_lines if item]
        if not raw_lines:
            return
        interesting: list[str] = []
        for index, line in enumerate(raw_lines):
            low = line.lower()
            if any(
                marker in low
                for marker in (
                    "comparing failed",
                    "compare script output",
                    "expected one of 42/43",
                    "testcase_run.sh",
                    "fail ",
                    "fail:",
                    "internal error",
                )
            ):
                interesting.extend(
                    item
                    for item in raw_lines[
                        max(0, index - 1) : min(len(raw_lines), index + 2)
                    ]
                    if item
                )
        if not interesting:
            interesting = raw_lines[-8:]
        for line in interesting:
            append_text(line)
            if len(lines) >= 16:
                return

    def walk_scalars(value: object, *, key_name: str = "") -> list[str]:
        out: list[str] = []
        key_token = domjudge_lower_text(key_name)
        if key_token in handled_keys or key_token == "disabled":
            return out
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                out.extend(
                    walk_scalars(sub_value, key_name=domjudge_text(sub_key))
                )
                if len(out) >= 32:
                    break
            return out
        if isinstance(value, list):
            for item in value:
                out.extend(walk_scalars(item, key_name=key_name))
                if len(out) >= 32:
                    break
            return out
        decoded = _decode_maybe_base64("" if value is None else str(value))
        if not decoded or _looks_like_raw_base64(decoded):
            return out
        normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
        for raw_line in normalized.split("\n"):
            text = domjudge_text(raw_line)
            if not text:
                continue
            low = text.lower()
            if any(marker in low for marker in interesting_markers):
                out.append(text)
            if len(out) >= 32:
                break
        return out

    for key in handled_keys:
        if key not in payload:
            continue
        value = payload[key]
        text = "" if value is None else str(value)
        if key == "judgehostlog":
            append_judgehost_log(text)
        else:
            append_text(text)
        if len(lines) >= 16:
            break
    if len(lines) < 16:
        for text in walk_scalars(payload):
            append_text(text)
            if len(lines) >= 16:
                break
    compact = "\n".join(lines)
    if len(compact) > 4000:
        compact = compact[:4000].rstrip()
    return ParsedDiagnosticPayload(text=compact)
