from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path


MAX_DIFF_BYTES = 512 * 1024
MAX_DIFF_LINES = 5_000
MAX_INLINE_DIFF_CHARS = 4_096


@dataclass(frozen=True)
class MergeDiffSegment:
    text: str
    changed: bool


@dataclass(frozen=True)
class MergeDiffCell:
    line_number: int | None
    segments: tuple[MergeDiffSegment, ...]
    no_newline: bool = False


@dataclass(frozen=True)
class MergeDiffRow:
    operation: str
    left: MergeDiffCell | None
    right: MergeDiffCell | None


@dataclass(frozen=True)
class MergeDiffSide:
    label: str
    exists: bool
    size: int
    executable: bool
    open_side: str


@dataclass(frozen=True)
class MergeComparison:
    path: str
    change_kind: str
    binary: bool
    truncated: bool
    message: str
    left: MergeDiffSide
    right: MergeDiffSide
    rows: tuple[MergeDiffRow, ...]


@dataclass(frozen=True)
class _TextFile:
    lines: tuple[str, ...]
    final_newline: bool


def _read_text(path: Path | None, size: int) -> tuple[_TextFile | None, bool, bool, str]:
    if path is None:
        return _TextFile((), True), False, False, ""
    if size > MAX_DIFF_BYTES:
        return None, False, True, "Text comparison is unavailable because this file is larger than 512 KiB."
    payload = path.read_bytes()
    if b"\0" in payload:
        return None, True, False, "Binary files cannot be compared as text."
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, True, False, "This file is not valid UTF-8 and cannot be compared as text."
    lines = tuple(text.splitlines())
    if len(lines) > MAX_DIFF_LINES:
        return None, False, True, "Text comparison is unavailable because this file has more than 5,000 lines."
    return _TextFile(lines, (not text) or text.endswith(("\n", "\r"))), False, False, ""


def _segments(left: str, right: str) -> tuple[tuple[MergeDiffSegment, ...], tuple[MergeDiffSegment, ...]]:
    if len(left) > MAX_INLINE_DIFF_CHARS or len(right) > MAX_INLINE_DIFF_CHARS:
        return (MergeDiffSegment(left, True),), (MergeDiffSegment(right, True),)
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    left_rows: list[MergeDiffSegment] = []
    right_rows: list[MergeDiffSegment] = []
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        changed = operation != "equal"
        if left_start != left_end:
            left_rows.append(MergeDiffSegment(left[left_start:left_end], changed))
        if right_start != right_end:
            right_rows.append(MergeDiffSegment(right[right_start:right_end], changed))
    return tuple(left_rows), tuple(right_rows)


def _cell(text: str, line_number: int, *, changed: bool, no_newline: bool) -> MergeDiffCell:
    return MergeDiffCell(line_number, (MergeDiffSegment(text, changed),), no_newline)


def _rows(left: _TextFile, right: _TextFile) -> tuple[MergeDiffRow, ...]:
    matcher = SequenceMatcher(None, left.lines, right.lines, autojunk=False)
    rows: list[MergeDiffRow] = []
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            for offset, (left_text, right_text) in enumerate(
                zip(left.lines[left_start:left_end], right.lines[right_start:right_end])
            ):
                left_number = left_start + offset + 1
                right_number = right_start + offset + 1
                rows.append(
                    MergeDiffRow(
                        "equal",
                        _cell(
                            left_text,
                            left_number,
                            changed=False,
                            no_newline=(left_number == len(left.lines) and not left.final_newline),
                        ),
                        _cell(
                            right_text,
                            right_number,
                            changed=False,
                            no_newline=(right_number == len(right.lines) and not right.final_newline),
                        ),
                    )
                )
            continue
        if operation == "delete":
            for index in range(left_start, left_end):
                number = index + 1
                rows.append(
                    MergeDiffRow(
                        "delete",
                        _cell(
                            left.lines[index],
                            number,
                            changed=True,
                            no_newline=(number == len(left.lines) and not left.final_newline),
                        ),
                        None,
                    )
                )
            continue
        if operation == "insert":
            for index in range(right_start, right_end):
                number = index + 1
                rows.append(
                    MergeDiffRow(
                        "insert",
                        None,
                        _cell(
                            right.lines[index],
                            number,
                            changed=True,
                            no_newline=(number == len(right.lines) and not right.final_newline),
                        ),
                    )
                )
            continue
        left_block = left.lines[left_start:left_end]
        right_block = right.lines[right_start:right_end]
        for offset, pair in enumerate(zip_longest(left_block, right_block)):
            left_text, right_text = pair
            left_cell: MergeDiffCell | None = None
            right_cell: MergeDiffCell | None = None
            if left_text is not None and right_text is not None:
                left_segments, right_segments = _segments(left_text, right_text)
                left_number = left_start + offset + 1
                right_number = right_start + offset + 1
                left_cell = MergeDiffCell(
                    left_number,
                    left_segments,
                    left_number == len(left.lines) and not left.final_newline,
                )
                right_cell = MergeDiffCell(
                    right_number,
                    right_segments,
                    right_number == len(right.lines) and not right.final_newline,
                )
            elif left_text is not None:
                left_number = left_start + offset + 1
                left_cell = _cell(
                    left_text,
                    left_number,
                    changed=True,
                    no_newline=(left_number == len(left.lines) and not left.final_newline),
                )
            elif right_text is not None:
                right_number = right_start + offset + 1
                right_cell = _cell(
                    right_text,
                    right_number,
                    changed=True,
                    no_newline=(right_number == len(right.lines) and not right.final_newline),
                )
            rows.append(MergeDiffRow("replace", left_cell, right_cell))
    return tuple(rows)


def compare_merge_files(
    *,
    path: str,
    change_kind: str,
    left_path: Path | None,
    left_side: MergeDiffSide,
    right_path: Path | None,
    right_side: MergeDiffSide,
) -> MergeComparison:
    left_text, left_binary, left_truncated, left_message = _read_text(left_path, left_side.size)
    right_text, right_binary, right_truncated, right_message = _read_text(right_path, right_side.size)
    binary = left_binary or right_binary
    truncated = left_truncated or right_truncated
    message = left_message or right_message
    if left_text is None or right_text is None:
        return MergeComparison(
            path,
            change_kind,
            binary,
            truncated,
            message,
            left_side,
            right_side,
            (),
        )
    return MergeComparison(
        path,
        change_kind,
        False,
        False,
        "",
        left_side,
        right_side,
        _rows(left_text, right_text),
    )
