from __future__ import annotations

import shutil
import shlex
import stat
import uuid
from pathlib import Path
from typing import TypedDict

import yaml


_LANGUAGE_CODES = {
    "english": "en",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "portuguese": "pt",
}

_SOURCE_LANGUAGE = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".py": "python3",
    ".java": "java",
}


class SubmissionRule(TypedDict):
    directory: str
    permitted: tuple[str, ...]
    required: tuple[str, ...]
    domjudge_results: tuple[str, ...]


SUBMISSION_RULES: dict[str, SubmissionRule] = {
    "accepted": {
        "directory": "accepted",
        "permitted": ("AC",),
        "required": ("AC",),
        "domjudge_results": ("CORRECT",),
    },
    "wrong_answer": {
        "directory": "wrong_answer",
        "permitted": ("AC", "WA"),
        "required": ("WA",),
        "domjudge_results": ("CORRECT", "WRONG-ANSWER"),
    },
    "time_limit_exceeded": {
        "directory": "time_limit_exceeded",
        "permitted": ("AC", "TLE"),
        "required": ("TLE",),
        "domjudge_results": ("CORRECT", "TIMELIMIT"),
    },
    "run_time_error": {
        "directory": "run_time_error",
        "permitted": ("AC", "RTE"),
        "required": ("RTE",),
        "domjudge_results": ("CORRECT", "RUN-ERROR"),
    },
    "tle_or_correct": {
        "directory": "mixed_tle_or_correct",
        "permitted": ("AC", "TLE"),
        "required": ("AC", "TLE"),
        "domjudge_results": ("CORRECT", "TIMELIMIT"),
    },
    "tle_or_re": {
        "directory": "mixed_tle_or_re",
        "permitted": ("TLE", "RTE"),
        "required": ("TLE", "RTE"),
        "domjudge_results": ("TIMELIMIT", "RUN-ERROR"),
    },
    "rejected": {
        "directory": "rejected",
        "permitted": ("AC", "WA", "TLE", "RTE"),
        "required": ("WA", "TLE", "RTE"),
        "domjudge_results": ("CORRECT", "WRONG-ANSWER", "TIMELIMIT", "RUN-ERROR"),
    },
}


def statement_language_code(language: str) -> str:
    token = language.strip().replace("_", "-")
    mapped = _LANGUAGE_CODES.get(token.lower())
    if mapped is not None:
        return mapped
    parts = token.split("-")
    if len(parts[0]) not in {2, 3} or not parts[0].isalpha():
        raise ValueError(f"unsupported statement language: {language}")
    if len(parts) == 1:
        return parts[0].lower()
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return f"{parts[0].lower()}-{parts[1].upper()}"
    raise ValueError(f"unsupported statement language: {language}")


def problem_uuid(problem_slug: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"polygon-replica/problem/{problem_slug}"))


def problem_type(*, mode: str, pass_limit: int) -> tuple[str, str]:
    if mode == "interactive" and pass_limit > 1:
        return ("legacy", "interactive multi-pass")
    if mode == "interactive":
        return ("2025-09", "interactive")
    if pass_limit > 1:
        return ("2025-09", "multi-pass")
    return ("2025-09", "pass-fail")


def render_problem_yaml(
    *,
    problem_slug: str,
    source_commit: str,
    names: dict[str, str],
    mode: str,
    pass_limit: int,
    time_limit_ms: int,
    memory_limit_mb: int | None,
) -> str:
    if not names:
        raise ValueError("ICPC export requires at least one problem statement")
    format_version, type_value = problem_type(mode=mode, pass_limit=pass_limit)
    name_value: str | dict[str, str]
    if list(names) == ["en"]:
        name_value = names["en"]
    else:
        name_value = names
    limits: dict[str, object] = {
        "time_limit": max(0.001, time_limit_ms / 1000.0),
    }
    if memory_limit_mb is not None:
        limits["memory"] = memory_limit_mb
    if pass_limit > 1:
        limits["validation_passes"] = pass_limit
    payload: dict[str, object] = {
        "problem_format_version": format_version,
        "type": type_value,
        "name": name_value,
        "uuid": problem_uuid(problem_slug),
        "version": source_commit,
        "limits": limits,
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


def render_submissions_yaml(entries: dict[str, dict[str, object]]) -> str:
    return yaml.safe_dump(
        entries,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


def source_language(path: Path) -> str:
    language = _SOURCE_LANGUAGE.get(path.suffix.lower())
    if language is None:
        raise ValueError(f"unsupported ICPC program language: {path.suffix or path.name}")
    return language


def annotated_submission(source: Path, results: tuple[str, ...]) -> bytes:
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError(f"submission source is not UTF-8: {source.name}") from exc
    if "@EXPECTED_RESULTS@:" in text.upper():
        raise ValueError(f"submission already contains @EXPECTED_RESULTS@: {source.name}")
    comment = "#" if source.suffix.lower() == ".py" else "//"
    annotation = f"{comment} @EXPECTED_RESULTS@: {','.join(results)}\n"
    return (annotation + text).encode("utf-8")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_testlib(snapshot: Path, target_dir: Path) -> None:
    testlib = snapshot / "third_party" / "testlib" / "testlib.h"
    if testlib.is_symlink() or not testlib.is_file():
        raise ValueError("export missing testlib header: third_party/testlib/testlib.h")
    shutil.copy2(testlib, target_dir / "testlib.h")


def _program_command(source: Path, *, snapshot: Path, target_dir: Path) -> tuple[str, str]:
    suffix = source.suffix.lower()
    shutil.copy2(source, target_dir / source.name)
    quoted_name = shlex.quote(source.name)
    if suffix in {".cc", ".cpp", ".cxx"}:
        _copy_testlib(snapshot, target_dir)
        return (
            f"c++ -Wall -DDOMJUDGE -O2 -std=gnu++20 -o program.bin -- {quoted_name}\n",
            "./program.bin",
        )
    if suffix == ".c":
        return (f"cc -Wall -O2 -o program.bin -- {quoted_name}\n", "./program.bin")
    if suffix == ".py":
        return ("", f"python3 {quoted_name}")
    raise ValueError(f"unsupported ICPC validator language: {suffix or source.name}")


def write_input_validator(*, snapshot: Path, package_root: Path, source: Path | None) -> None:
    target_dir = package_root / "input_validators" / ("validator" if source is not None else "accept_all")
    target_dir.mkdir(parents=True, exist_ok=True)
    if source is None:
        _write_executable(target_dir / "run", "#!/bin/sh\ncat >/dev/null\nexit 42\n")
        return
    build_command, command = _program_command(source, snapshot=snapshot, target_dir=target_dir)
    if build_command:
        _write_executable(target_dir / "build", f"#!/bin/sh\nset -eu\n{build_command}")
    _write_executable(
        target_dir / "run",
        "#!/bin/sh\n"
        f"{command} \"$@\"\n"
        "status=$?\n"
        "case \"$status\" in\n"
        "  0|42) exit 42 ;;\n"
        "  *) exit \"$status\" ;;\n"
        "esac\n",
    )


def write_output_validator(*, snapshot: Path, package_root: Path, source: Path | None) -> None:
    if source is None:
        return
    target_dir = package_root / "output_validator"
    target_dir.mkdir(parents=True, exist_ok=True)
    build_command, command = _program_command(source, snapshot=snapshot, target_dir=target_dir)
    if build_command:
        _write_executable(target_dir / "build", f"#!/bin/sh\nset -eu\n{build_command}")
    _write_executable(
        target_dir / "run",
        "#!/bin/sh\n"
        f"{command} \"$@\"\n"
        "status=$?\n"
        "case \"$status\" in\n"
        "  0|42) exit 42 ;;\n"
        "  1|2|43) exit 43 ;;\n"
        "  *) exit \"$status\" ;;\n"
        "esac\n",
    )
