"""Validate optional testlib markup against the exact displayed sample bytes."""

import logging


_LOG = logging.getLogger(__name__)
_HEADER = b"MU\xf3\x01"


def sample_shaded_lines(markup: bytes, content: bytes) -> tuple[int, ...]:
    """Return one-based shaded lines; unusable optional markup never breaks rendering."""
    try:
        return _sample_shaded_lines(markup, content)
    except ValueError as exc:
        _LOG.warning("Ignoring sample markup: %s", exc)
        return ()


def _sample_shaded_lines(markup: bytes, content: bytes) -> tuple[int, ...]:
    if len(markup) > 32 * (len(content) + 1) or not markup.startswith(_HEADER):
        raise ValueError("invalid header or oversized markup")
    restored = bytearray()
    boundaries: list[tuple[int, int]] = []
    cursor = len(_HEADER)
    previous_case = 0
    while cursor < len(markup):
        byte = markup[cursor]
        cursor += 1
        if byte == 92:
            if cursor == len(markup) or markup[cursor] not in (92, 33):
                raise ValueError("invalid escape")
            restored.append(markup[cursor])
            cursor += 1
        elif byte == 33:
            end = markup.find(b";", cursor, cursor + 12)
            if end < 0 or markup[cursor:cursor + 1] != b"c":
                raise ValueError("invalid case marker")
            digits = markup[cursor + 1:end]
            if not digits or not digits.isdigit() or digits.startswith(b"0"):
                raise ValueError("invalid case number")
            case = int(digits)
            if case <= previous_case or case > 1073741823:
                raise ValueError("unordered case markers")
            boundaries.append((len(restored), case))
            previous_case = case
            cursor = end + 1
        else:
            restored.append(byte)
        if len(restored) > len(content) or len(boundaries) > len(content) + 1:
            raise ValueError("markup exceeds sample bounds")
    if restored != content:
        raise ValueError("markup does not match displayed content")
    if not boundaries:
        return ()
    # Token readers may leave the previous line's trailing whitespace unread.
    aligned: list[tuple[int, int]] = []
    for offset, case in boundaries:
        if offset and content[offset - 1:offset] != b"\n":
            end = content.find(b"\n", offset)
            if end < 0 or content[offset:end].strip(b" \t\r"):
                raise ValueError("case boundary is inside a line")
            offset = end + 1
        if offset >= len(content) or (aligned and offset <= aligned[-1][0]):
            raise ValueError("empty or overlapping case range")
        aligned.append((offset, case))
    shaded: list[int] = []
    active = 0
    index = 0
    offset = 0
    # LF is the sample line separator; preserve CR and all other bytes.
    lines = content.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if index < len(aligned) and aligned[index][0] == offset:
            active = aligned[index][1]
            index += 1
        if active % 2:
            shaded.append(number)
        offset += len(line) + 1
    return tuple(shaded)
