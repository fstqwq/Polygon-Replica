"""Canonical ``solutions/<source>.desc`` model and codec."""

from pathlib import Path
from typing import Literal, TypedDict, cast

from app.service.problem.source_file import require_regular_source_file

ExpectedBehavior = Literal[
    "accepted",
    "wrong_answer",
    "tle_or_correct",
    "tle_or_re",
    "time_limit_exceeded",
    "run_time_error",
    "compile_error",
    "rejected",
    "unknown",
]

EXPECTED_BEHAVIOR_VALUES: tuple[ExpectedBehavior, ...] = (
    "accepted",
    "wrong_answer",
    "tle_or_correct",
    "tle_or_re",
    "time_limit_exceeded",
    "run_time_error",
    "compile_error",
    "rejected",
    "unknown",
)
_EXPECTED_BEHAVIOR_SET = frozenset(EXPECTED_BEHAVIOR_VALUES)

EXPECTED_BEHAVIOR_LABELS: dict[ExpectedBehavior, str] = {
    "accepted": "accepted (AC)",
    "wrong_answer": "wrong_answer (WA)",
    "tle_or_correct": "tle_or_correct (TL/AC)",
    "tle_or_re": "tle_or_re (TL/RE)",
    "time_limit_exceeded": "time_limit_exceeded (TL)",
    "run_time_error": "run_time_error (RE)",
    "compile_error": "compile_error (CE)",
    "rejected": "rejected",
    "unknown": "unknown",
}


class SolutionDescriptor(TypedDict):
    expected_behavior: ExpectedBehavior
    note: str


def normalize_expected_behavior(raw: str) -> ExpectedBehavior:
    """Validate a canonical behavior token received at an authoring boundary."""

    if not isinstance(raw, str):
        raise ValueError("expected behavior must be a string")
    token = raw
    if token not in _EXPECTED_BEHAVIOR_SET:
        raise ValueError(f"unknown expected behavior '{token}'")
    return cast(ExpectedBehavior, token)


def expected_behavior_label(value: str) -> str:
    return EXPECTED_BEHAVIOR_LABELS[normalize_expected_behavior(value)]


def desc_rel_path_for_source(source_rel: str) -> str:
    return f"{source_rel}.desc"


def parse_solution_desc(
    text: str,
    *,
    label: str = "solution descriptor",
) -> SolutionDescriptor:
    if not isinstance(text, str):
        raise ValueError(f"{label}: content must be UTF-8 text")
    expected: ExpectedBehavior | None = None
    notes: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line:
            continue
        if ":" not in raw_line:
            raise ValueError(f"{label}:{line_number}: expected 'key: value'")
        raw_key, raw_value = raw_line.split(":", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if key == "expected":
            if expected is not None:
                raise ValueError(f"{label}:{line_number}: duplicate expected field")
            try:
                expected = normalize_expected_behavior(value)
            except ValueError as exc:
                raise ValueError(f"{label}:{line_number}: {exc}") from exc
            continue
        if key == "note":
            if not value:
                raise ValueError(f"{label}:{line_number}: note must not be empty")
            notes.append(value)
            continue
        raise ValueError(f"{label}:{line_number}: unsupported key '{key}'")
    if expected is None:
        raise ValueError(f"{label}: missing expected field")
    return {"expected_behavior": expected, "note": "\n".join(notes)}


def load_solution_desc(root: Path, source_rel: str) -> SolutionDescriptor:
    label = desc_rel_path_for_source(source_rel)
    unresolved = root / label
    if not unresolved.is_symlink() and not unresolved.exists():
        return {"expected_behavior": "unknown", "note": ""}
    path = require_regular_source_file(root, label)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}: must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"{label}: cannot read file: {exc}") from exc
    return parse_solution_desc(text, label=label)


def render_solution_desc(expected_behavior: str, note: str = "") -> str:
    normalized = normalize_expected_behavior(expected_behavior)
    lines = [f"expected: {normalized}"]
    if not isinstance(note, str):
        raise ValueError("solution note must be a string")
    for raw_line in note.splitlines():
        piece = raw_line.strip()
        if piece:
            lines.append(f"note: {piece}")
    return "\n".join(lines) + "\n"
