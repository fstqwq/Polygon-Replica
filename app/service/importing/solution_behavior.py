"""Best-effort solution behavior inference for external package imports."""

from pathlib import Path
from typing import cast

from app.service.problem.solution_metadata import ExpectedBehavior

_CANONICAL = {
    "accepted",
    "wrong_answer",
    "tle_or_correct",
    "tle_or_re",
    "time_limit_exceeded",
    "run_time_error",
    "compile_error",
    "rejected",
    "unknown",
}
_ALIASES: dict[str, ExpectedBehavior] = {
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
    "ce": "compile_error",
    "compile": "compile_error",
    "rej": "rejected",
    "reject": "rejected",
}

_POLYGON_TAG_BEHAVIOR: dict[str, ExpectedBehavior] = {
    "main": "accepted",
    "accepted": "accepted",
    "wrong-answer": "wrong_answer",
    "presentation-error": "wrong_answer",
    "time-limit-exceeded": "time_limit_exceeded",
    "time-limit-exceeded-or-accepted": "tle_or_correct",
    "time-limit-exceeded-or-memory-limit-exceeded": "tle_or_re",
    "memory-limit-exceeded": "run_time_error",
    "compilation-error": "compile_error",
    "compile-error": "compile_error",
    "rejected": "rejected",
    "failed": "rejected",
    "do-not-run": "unknown",
}
_POLYGON_SOLUTION_SUFFIXES = {".cpp", ".cc", ".cxx", ".c++", ".py", ".java"}


def _token(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_").replace("-", "_")


def external_expected_behavior(raw: str) -> ExpectedBehavior:
    token = _token(raw)
    if token in _CANONICAL:
        return cast(ExpectedBehavior, token)
    direct = _ALIASES.get(token)
    if direct is not None:
        return direct
    return _ALIASES.get(token.replace("_", ""), "unknown")


def polygon_solution_expected_from_tag(tag: str) -> ExpectedBehavior:
    if not tag:
        return "unknown"
    direct = _POLYGON_TAG_BEHAVIOR.get(tag)
    if direct is not None:
        return direct
    return external_expected_behavior(tag)


def polygon_solution_filename(source_path: str, source_type: str) -> str:
    safe_name = Path(source_path).name or "solution"
    if ("python" in source_type) or ("pypy" in source_type):
        expected_suffix = ".py"
    elif "java" in source_type:
        expected_suffix = ".java"
    elif any(token in source_type for token in ("cpp", "c++", "g++", "clang++")):
        expected_suffix = ".cpp"
    else:
        return safe_name
    if safe_name.lower().endswith(expected_suffix):
        return safe_name
    current_suffix = Path(safe_name).suffix.lower()
    if not current_suffix or current_suffix in _POLYGON_SOLUTION_SUFFIXES:
        return f"{safe_name}{expected_suffix}"
    return safe_name
