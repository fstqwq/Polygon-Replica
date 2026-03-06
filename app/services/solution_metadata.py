from __future__ import annotations

import re
from pathlib import Path


def _normalize_behavior_token(raw: str) -> str:
    return str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")


EXPECTED_BEHAVIOR_VALUES = [
    "accepted",
    "wrong_answer",
    "tle_or_correct",
    "tle_or_re",
    "time_limit_exceeded",
    "run_time_error",
    "rejected",
    "unknown",
]

EXPECTED_BEHAVIOR_LABELS = {
    "accepted": "accepted (AC)",
    "wrong_answer": "wrong_answer (WA)",
    "tle_or_correct": "tle_or_correct (TL/AC)",
    "tle_or_re": "tle_or_re (TL/RE)",
    "time_limit_exceeded": "time_limit_exceed (TL)",
    "run_time_error": "run_time_error (RE)",
    "rejected": "rejected",
    "unknown": "unknown",
}

EXPECTED_BEHAVIOR_ALIASES = {
    "accepted": "accepted",
    "wrong_answer": "wrong_answer",
    "tle_or_correct": "tle_or_correct",
    "tle_or_re": "tle_or_re",
    "time_limit_exceeded": "time_limit_exceeded",
    "run_time_error": "run_time_error",
    "rejected": "rejected",
    "unknown": "unknown",
}

# Filename-only shorthand used when creating/importing solution metadata.
# Runtime verification does not consult these aliases.
EXPECTED_BEHAVIOR_FILENAME_ALIASES = {
    "ac": "accepted",
    "main": "accepted",
    "wa": "wrong_answer",
    "tlac": "tle_or_correct",
    "tle_or_ac": "tle_or_correct",
    "tleorac": "tle_or_correct",
    "tlre": "tle_or_re",
    "tle_or_re": "tle_or_re",
    "tleorre": "tle_or_re",
    "tl": "time_limit_exceeded",
    "tle": "time_limit_exceeded",
    "bf": "time_limit_exceeded",
    "bruteforce": "time_limit_exceeded",
    "brute_force": "time_limit_exceeded",
    "re": "run_time_error",
    "rte": "run_time_error",
    "mle": "run_time_error",
    "rej": "rejected",
    "reject": "rejected",
}


def normalize_expected_behavior(raw: str) -> str:
    token = _normalize_behavior_token(raw)
    if not token:
        return "unknown"
    return EXPECTED_BEHAVIOR_ALIASES.get(token, "unknown")


def _infer_behavior_from_filename_token(raw: str) -> str:
    token = _normalize_behavior_token(raw)
    if not token:
        return "unknown"
    direct = normalize_expected_behavior(token)
    if direct != "unknown":
        return direct
    alias = EXPECTED_BEHAVIOR_FILENAME_ALIASES.get(token)
    if alias:
        return alias
    compact = token.replace("_", "")
    alias = EXPECTED_BEHAVIOR_FILENAME_ALIASES.get(compact)
    if alias:
        return alias
    return "unknown"


def expected_behavior_label(value: str) -> str:
    normalized = normalize_expected_behavior(value)
    return str(EXPECTED_BEHAVIOR_LABELS.get(normalized, EXPECTED_BEHAVIOR_LABELS["unknown"]))


def desc_rel_path_for_source(source_rel: str) -> str:
    return f"{source_rel}.desc"


def infer_expected_behavior_from_name(source_rel: str) -> str:
    stem = Path(str(source_rel or "")).stem.lower()
    direct = _infer_behavior_from_filename_token(stem)
    if direct != "unknown":
        return direct
    for token in re.split(r"[^a-z0-9]+", stem):
        if not token:
            continue
        guess = _infer_behavior_from_filename_token(token)
        if guess != "unknown":
            return guess
    return "unknown"


def parse_solution_desc(text: str) -> dict:
    expected_raw = ""
    note_lines: list[str] = []
    errors: list[str] = []

    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            note_lines.append(line)
            continue
        key, value = line.split(":", 1)
        key = str(key or "").strip().lower()
        value = str(value or "").strip()
        if key in {"expected", "behavior", "verdict"}:
            expected_raw = value
            continue
        if key == "note":
            if value:
                note_lines.append(value)
            continue
        errors.append(f"unknown key '{key}'")

    expected_behavior = normalize_expected_behavior(expected_raw)
    if expected_raw and expected_behavior == "unknown":
        errors.append(f"unknown expected behavior '{expected_raw}'")
    note = "\n".join(note_lines).strip()
    return {
        "expected_behavior": expected_behavior,
        "note": note,
        "errors": errors,
    }


def render_solution_desc(expected_behavior: str, note: str = "") -> str:
    normalized = normalize_expected_behavior(expected_behavior)
    lines = [f"expected: {normalized}"]
    clean_note = str(note or "").strip()
    if clean_note:
        for item in clean_note.splitlines():
            piece = str(item or "").rstrip()
            if piece:
                lines.append(f"note: {piece}")
    return "\n".join(lines) + "\n"
