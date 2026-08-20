"""Canonical expected-behavior parsing for ICPC package submissions."""

import os
import re
import uuid
from pathlib import Path, PurePosixPath

import yaml

from app.service.importing.solution_behavior import external_expected_behavior


_BEHAVIOR_BY_RULE: dict[tuple[frozenset[str], frozenset[str]], str] = {
    (frozenset({"AC"}), frozenset({"AC"})): "accepted",
    (frozenset({"AC", "WA"}), frozenset({"WA"})): "wrong_answer",
    (frozenset({"AC", "TLE"}), frozenset({"TLE"})): "time_limit_exceeded",
    (frozenset({"AC", "RTE"}), frozenset({"RTE"})): "run_time_error",
    (frozenset({"CE"}), frozenset({"CE"})): "compile_error",
    (frozenset({"AC", "TLE"}), frozenset({"AC", "TLE"})): "tle_or_correct",
    (frozenset({"TLE", "RTE"}), frozenset({"TLE", "RTE"})): "tle_or_re",
    (
        frozenset({"AC", "WA", "TLE", "RTE"}),
        frozenset({"WA", "TLE", "RTE"}),
    ): "rejected",
}

_DOMJUDGE_RESULT_TO_PPF = {
    "CORRECT": "AC",
    "WRONG-ANSWER": "WA",
    "TIMELIMIT": "TLE",
    "RUN-ERROR": "RTE",
    "COMPILER-ERROR": "CE",
}

_ANNOTATION_BEHAVIOR_BY_RESULTS = {
    frozenset({"AC"}): "accepted",
    frozenset({"AC", "WA"}): "wrong_answer",
    frozenset({"AC", "TLE"}): "tle_or_correct",
    frozenset({"AC", "RTE"}): "run_time_error",
    frozenset({"CE"}): "compile_error",
    frozenset({"TLE", "RTE"}): "tle_or_re",
    frozenset({"WA", "TLE", "RTE", "CE"}): "rejected",
}


def submission_expected_from_group(raw_group: str) -> str:
    token = raw_group.strip().lower().replace("-", "_")
    direct = external_expected_behavior(token)
    if direct != "unknown":
        return direct
    aliases = {
        "wrong_answer": "wrong_answer",
        "time_limit_exceeded": "time_limit_exceeded",
        "tle": "time_limit_exceeded",
        "run_time_error": "run_time_error",
        "runtime_error": "run_time_error",
        "rte": "run_time_error",
        "compile_error": "compile_error",
        "ce": "compile_error",
        "accepted": "accepted",
        "ac": "accepted",
        "mixed_tle_or_correct": "tle_or_correct",
        "mixed_tle_or_re": "tle_or_re",
        "mixed_rejected": "rejected",
        "mixed": "unknown",
        "rejected": "rejected",
        "reject": "rejected",
    }
    return aliases.get(token, "unknown")


def _verdict_set(raw: object, *, field: str) -> frozenset[str]:
    if not isinstance(raw, list):
        raise ValueError(f"submissions.yaml {field} must be a sequence")
    tokens: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in {"AC", "WA", "TLE", "RTE", "CE"}:
            raise ValueError(f"submissions.yaml contains invalid {field} verdict")
        tokens.append(item)
    if len(tokens) != len(set(tokens)):
        raise ValueError(f"submissions.yaml {field} contains duplicate verdicts")
    return frozenset(tokens)


def _archive_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ValueError("submissions.yaml contains an invalid submission path")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("submissions.yaml contains an unsafe submission path")
    parts = rel.parts[1:] if rel.parts[0] == "submissions" else rel.parts
    if not parts:
        raise ValueError("submissions.yaml contains an invalid submission path")
    return PurePosixPath("submissions", *parts).as_posix()


def parse_submissions_yaml(text: str) -> tuple[dict[str, str], list[str]]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid submissions/submissions.yaml: {exc}") from exc
    if loaded is None:
        return ({}, [])
    if not isinstance(loaded, dict):
        raise ValueError("submissions/submissions.yaml must contain a mapping")
    behaviors: dict[str, str] = {}
    warnings: list[str] = []
    for raw_path, raw_rule in loaded.items():
        archive_path = _archive_path(raw_path)
        if archive_path in behaviors:
            raise ValueError("submissions/submissions.yaml contains a duplicate submission path")
        if not isinstance(raw_rule, dict):
            raise ValueError("submissions/submissions.yaml submission rule must be a mapping")
        permitted = _verdict_set(raw_rule.get("permitted"), field="permitted")
        required = _verdict_set(raw_rule.get("required"), field="required")
        if not permitted or not required.issubset(permitted):
            raise ValueError("submissions/submissions.yaml contains an invalid verdict rule")
        behavior = _BEHAVIOR_BY_RULE.get((permitted, required), "unknown")
        behaviors[archive_path] = behavior
        if behavior == "unknown":
            warnings.append(
                f"{archive_path}: submissions.yaml verdict rule is not representable"
            )
    return (behaviors, warnings)


def consume_generated_expected_results(path: Path) -> tuple[str | None, str]:
    """Read and remove a DOMjudge expected-results first-line annotation."""

    with path.open("rb") as handle:
        first_line = handle.readline(4097)
    if len(first_line) > 4096 and not first_line.endswith(b"\n"):
        if first_line.startswith(
            (b"# @EXPECTED_RESULTS@:", b"// @EXPECTED_RESULTS@:")
        ):
            raise ValueError("submission annotation contains invalid expected results")
        return (None, "")
    match = re.fullmatch(
        rb"(?:#|//) @EXPECTED_RESULTS@: ([A-Z-]+(?:,[A-Z-]+)*)\r?\n?",
        first_line,
    )
    if match is None:
        if first_line.startswith(
            (b"# @EXPECTED_RESULTS@:", b"// @EXPECTED_RESULTS@:")
        ):
            raise ValueError("submission annotation contains invalid expected results")
        return (None, "")
    raw_results = match.group(1).decode("ascii").split(",")
    try:
        verdicts = frozenset(_DOMJUDGE_RESULT_TO_PPF[result] for result in raw_results)
    except KeyError as exc:
        raise ValueError("submission annotation contains an invalid expected result") from exc
    if len(verdicts) != len(raw_results):
        raise ValueError("submission annotation contains duplicate expected results")
    behavior = _ANNOTATION_BEHAVIOR_BY_RESULTS.get(verdicts, "unknown")
    warning = "" if behavior != "unknown" else "submission annotation result set is not representable"
    temporary = path.with_name(f".{path.name}.annotation-{uuid.uuid4().hex}")
    try:
        with path.open("rb") as source, temporary.open("wb") as destination:
            source.seek(len(first_line))
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return (behavior, warning)
