import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import BinaryIO, Literal, TypedDict


RunpipeTranscriptSource = Literal["interactor", "solution"]
RunpipeTranscriptState = Literal["ok", "malformed", "limited"]

RUNPIPE_TRANSCRIPT_SCAN_LIMIT = 1000


class RunpipeTranscriptEvent(TypedDict):
    kind: Literal["data", "eof"]
    source: RunpipeTranscriptSource
    timestamp_seconds: Decimal
    payload_display: str
    payload_bytes: int
    payload_bytes_shown: int
    payload_bytes_omitted: int


class RunpipeTranscript(TypedDict):
    state: RunpipeTranscriptState
    events: list[RunpipeTranscriptEvent]
    events_shown: int
    events_total: int | None
    events_omitted: int | None
    raw_size_bytes: int
    error_offset: int | None
    error_reason: str | None


_METADATA_RE = re.compile(rb"\[(?P<seconds> *[0-9]+\.[0-9]{3})s/(?P<payload_bytes>[0-9]+)\]")
_HEADER_LIMIT = 128
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


class _MalformedTranscript(ValueError):
    def __init__(self, offset: int, reason: str) -> None:
        super().__init__(reason)
        self.offset = offset
        self.reason = reason


def _read_exact(stream: BinaryIO, length: int, *, offset: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise _MalformedTranscript(offset + length - remaining, f"truncated {label}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_metadata(
    stream: BinaryIO,
    *,
    offset: int,
) -> tuple[Decimal, int, int]:
    metadata = bytearray(b"[")
    while len(metadata) < _HEADER_LIMIT:
        token = stream.read(1)
        if not token:
            raise _MalformedTranscript(offset, "truncated event header")
        metadata.extend(token)
        if token == b"]":
            break
    else:
        raise _MalformedTranscript(offset, "event header is too long")

    match = _METADATA_RE.fullmatch(bytes(metadata))
    if match is None:
        raise _MalformedTranscript(offset, "invalid event header")
    try:
        timestamp = Decimal(match.group("seconds").decode("ascii").strip())
    except (InvalidOperation, UnicodeDecodeError) as exc:
        raise _MalformedTranscript(offset, "invalid event timestamp") from exc
    payload_bytes = int(match.group("payload_bytes"))
    return timestamp, payload_bytes, len(metadata)


def _visible_payload(payload: bytes) -> str:
    if not payload:
        return "(empty)"
    decoded = payload.decode("utf-8", errors="backslashreplace")
    visible: list[str] = []
    for char in decoded:
        codepoint = ord(char)
        if char in {"\n", "\t"}:
            visible.append(char)
        elif codepoint in _BIDI_CONTROL_CODEPOINTS or unicodedata.category(char).startswith("C"):
            if codepoint <= 0xFF:
                visible.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                visible.append(f"\\u{codepoint:04x}")
            else:
                visible.append(f"\\U{codepoint:08x}")
        else:
            visible.append(char)
    return "".join(visible)


def _skip_payload(stream: BinaryIO, length: int, *, offset: int) -> None:
    if length <= 0:
        return
    remaining = length
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise _MalformedTranscript(offset + length - remaining, "truncated event payload")
        remaining -= len(chunk)


def parse_runpipe_transcript(
    stream: BinaryIO,
    *,
    raw_size_bytes: int,
    event_limit: int = 100,
    scan_limit: int = RUNPIPE_TRANSCRIPT_SCAN_LIMIT,
    payload_preview_limit: int = 8 * 1024,
) -> RunpipeTranscript:
    """Parse a DOMjudge runpipe transcript without interpreting payload bytes.

    The stream must start at offset zero. Parsing is anchored at the current
    record boundary; marker-looking bytes inside a payload are never scanned as
    protocol data. No seek or look-behind is required. If the scan limit is
    reached before the declared raw size, the result is limited and the unread
    suffix is intentionally left unvalidated.
    """

    if event_limit < 1:
        raise ValueError("event_limit must be positive")
    if scan_limit < 1:
        raise ValueError("scan_limit must be positive")
    if payload_preview_limit < 1:
        raise ValueError("payload_preview_limit must be positive")
    if raw_size_bytes < 0:
        raise ValueError("raw_size_bytes cannot be negative")

    events: list[RunpipeTranscriptEvent] = []
    event_total = 0
    try:
        offset = 0
        while offset < raw_size_bytes:
            if event_total >= scan_limit:
                return {
                    "state": "limited",
                    "events": events,
                    "events_shown": len(events),
                    "events_total": None,
                    "events_omitted": None,
                    "raw_size_bytes": raw_size_bytes,
                    "error_offset": None,
                    "error_reason": None,
                }
            event_offset = offset
            first = _read_exact(
                stream,
                1,
                offset=offset,
                label="event header",
            )
            offset += 1
            if first != b"[":
                raise _MalformedTranscript(event_offset, "event does not start with '['")
            timestamp, payload_bytes, metadata_bytes = _read_metadata(
                stream,
                offset=event_offset,
            )
            offset = event_offset + metadata_bytes
            direction_offset = offset
            direction = _read_exact(
                stream,
                1,
                offset=direction_offset,
                label="event direction",
            )
            offset += 1

            if direction in {b"]", b"["}:
                if payload_bytes != 0:
                    raise _MalformedTranscript(
                        event_offset, "EOF event has a non-zero payload length"
                    )
                source: RunpipeTranscriptSource = "interactor" if direction == b"]" else "solution"
                if event_total < event_limit:
                    events.append(
                        {
                            "kind": "eof",
                            "source": source,
                            "timestamp_seconds": timestamp,
                            "payload_display": "closed output",
                            "payload_bytes": 0,
                            "payload_bytes_shown": 0,
                            "payload_bytes_omitted": 0,
                        }
                    )
                event_total += 1
                continue

            if direction not in {b">", b"<"}:
                raise _MalformedTranscript(direction_offset, "invalid event direction")
            separator_offset = offset
            separator = _read_exact(
                stream,
                2,
                offset=separator_offset,
                label="event separator",
            )
            offset += 2
            if separator != b": ":
                raise _MalformedTranscript(separator_offset, "invalid event separator")

            source = "interactor" if direction == b">" else "solution"
            shown_length = (
                min(payload_bytes, payload_preview_limit) if event_total < event_limit else 0
            )
            payload_offset = offset
            bytes_available = raw_size_bytes - payload_offset
            if bytes_available < payload_bytes:
                raise _MalformedTranscript(payload_offset, "truncated event payload")
            shown_payload = _read_exact(
                stream,
                shown_length,
                offset=payload_offset,
                label="event payload",
            )
            _skip_payload(
                stream,
                payload_bytes - shown_length,
                offset=payload_offset + shown_length,
            )
            offset += payload_bytes
            delimiter_offset = offset
            if delimiter_offset >= raw_size_bytes:
                raise _MalformedTranscript(delimiter_offset, "truncated event delimiter")
            delimiter = _read_exact(
                stream,
                1,
                offset=delimiter_offset,
                label="event delimiter",
            )
            offset += 1
            if delimiter != b"\n":
                raise _MalformedTranscript(delimiter_offset, "invalid event delimiter")

            if event_total < event_limit:
                events.append(
                    {
                        "kind": "data",
                        "source": source,
                        "timestamp_seconds": timestamp,
                        "payload_display": _visible_payload(shown_payload),
                        "payload_bytes": payload_bytes,
                        "payload_bytes_shown": shown_length,
                        "payload_bytes_omitted": payload_bytes - shown_length,
                    }
                )
            event_total += 1
    except _MalformedTranscript as exc:
        return {
            "state": "malformed",
            "events": events,
            "events_shown": len(events),
            "events_total": event_total,
            "events_omitted": max(0, event_total - len(events)),
            "raw_size_bytes": raw_size_bytes,
            "error_offset": exc.offset,
            "error_reason": exc.reason,
        }

    return {
        "state": "ok",
        "events": events,
        "events_shown": len(events),
        "events_total": event_total,
        "events_omitted": max(0, event_total - len(events)),
        "raw_size_bytes": raw_size_bytes,
        "error_offset": None,
        "error_reason": None,
    }
