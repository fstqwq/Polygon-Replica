"""Canonical Contest problem-index validation and ordering."""

import re

from app.main_constant import CONTEST_IDENT_RE


_LETTERS_RE = re.compile(r"^[A-Z]+$")
_NATURAL_PART_RE = re.compile(r"\d+|\D+")

ContestProblemIndexSortPart = tuple[int, int, str]
ContestProblemIndexSortKey = tuple[
    int,
    int,
    tuple[ContestProblemIndexSortPart, ...],
    str,
]


def normalize_contest_problem_idx(raw: object) -> str:
    """Return the canonical public Contest problem index."""
    token = str(raw or "").strip().upper()
    if not token:
        raise ValueError("problem index is required")
    if len(token) > 16:
        raise ValueError("problem index is too long")
    if not CONTEST_IDENT_RE.fullmatch(token):
        raise ValueError("invalid problem index")
    return token


def _excel_column_number(token: str) -> int:
    value = 0
    for char in token:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def contest_problem_idx_sort_key(idx: str) -> ContestProblemIndexSortKey:
    """Sort conventional letter indices first, then custom indices naturally."""
    if _LETTERS_RE.fullmatch(idx):
        return (0, _excel_column_number(idx), (), idx)
    parts: list[ContestProblemIndexSortPart] = []
    for part in _NATURAL_PART_RE.findall(idx):
        if part.isdigit():
            parts.append((0, int(part), ""))
        else:
            parts.append((1, 0, part))
    return (1, 0, tuple(parts), idx)
