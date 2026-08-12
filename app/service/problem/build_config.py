"""Canonical ``config/build.json`` model and codec."""

import json
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict

from app.main_constant import (
    CPP_SOURCE_EXTENSIONS,
    SOLUTION_SOURCE_EXTENSIONS,
)
from app.service.problem.json_codec import (
    loads_object,
    reject_unknown_keys,
    require_keys,
)
from app.service.problem.source_file import require_regular_source_file

BUILD_CONFIG_REL = Path("config/build.json")
BUILD_CONFIG_KEY_ORDER: tuple[str, ...] = (
    "accepted_solution_source",
    "validator_source",
    "checker_source",
    "interactor_source",
    "generator_sources",
    "generator_runs",
    "generator_args",
    "validator_args",
    "checker_args",
    "compile_jobs",
    "validate_jobs",
    "solve_jobs",
    "run_jobs",
    "run_timeout_sec",
)
BUILD_CONFIG_KEYS = frozenset(BUILD_CONFIG_KEY_ORDER)
BUILD_CONFIG_REQUIRED_KEYS = frozenset(
    {
        "generator_sources",
        "generator_runs",
        "generator_args",
        "validator_args",
        "checker_args",
        "compile_jobs",
        "validate_jobs",
        "solve_jobs",
        "run_jobs",
        "run_timeout_sec",
    }
)


class BuildConfig(TypedDict):
    accepted_solution_source: NotRequired[str]
    validator_source: NotRequired[str]
    checker_source: NotRequired[str]
    interactor_source: NotRequired[str]
    generator_sources: list[str]
    generator_runs: int
    generator_args: list[str]
    validator_args: list[str]
    checker_args: list[str]
    compile_jobs: int
    validate_jobs: int
    solve_jobs: int
    run_jobs: int
    run_timeout_sec: int


def default_build_config() -> BuildConfig:
    """Construct the canonical source written for a newly-created problem."""

    return {
        "generator_sources": [],
        "generator_runs": 3,
        "generator_args": [],
        "validator_args": [],
        "checker_args": [],
        "compile_jobs": 0,
        "validate_jobs": 0,
        "solve_jobs": 0,
        "run_jobs": 0,
        "run_timeout_sec": 30,
    }


def _source_path(
    raw: object,
    *,
    key: str,
    root: str,
    extensions: set[str],
    label: str,
) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError(f"{label}.{key}: must be a non-empty string")
    if "\\" in raw or raw != raw.strip():
        raise ValueError(f"{label}.{key}: must be a normalized relative path")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise ValueError(f"{label}.{key}: must be a normalized relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or path.parts[0] != root:
        raise ValueError(f"{label}.{key}: must be below {root}/")
    if path.as_posix() != raw:
        raise ValueError(f"{label}.{key}: must be a normalized relative path")
    if len(path.parts) < 2:
        raise ValueError(f"{label}.{key}: source filename is required")
    if root == "solutions" and len(path.parts) != 2:
        raise ValueError(
            f"{label}.{key}: solution source must be directly below solutions/"
        )
    if path.suffix.lower() not in extensions:
        allowed = "/".join(sorted(extensions))
        raise ValueError(f"{label}.{key}: source must use one of: {allowed}")
    return path.as_posix()


def _string_array(raw: object, *, key: str, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{label}.{key}: must be an array of strings")
    values: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or "\x00" in item:
            raise ValueError(f"{label}.{key}[{index}]: must be a string")
        values.append(item)
    return values


def _integer(
    raw: object,
    *,
    key: str,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{label}.{key}: must be an integer")
    if raw < minimum or raw > maximum:
        raise ValueError(
            f"{label}.{key}: must be between {minimum} and {maximum}"
        )
    return raw


def parse_build_config(
    text: str,
    *,
    label: str = "config/build.json",
) -> BuildConfig:
    payload = loads_object(text, label=label)
    reject_unknown_keys(payload, allowed=BUILD_CONFIG_KEYS, label=label)
    require_keys(payload, required=BUILD_CONFIG_REQUIRED_KEYS, label=label)
    result = default_build_config()

    if "accepted_solution_source" in payload:
        result["accepted_solution_source"] = _source_path(
            payload["accepted_solution_source"],
            key="accepted_solution_source",
            root="solutions",
            extensions=SOLUTION_SOURCE_EXTENSIONS,
            label=label,
        )
    if "validator_source" in payload:
        result["validator_source"] = _source_path(
            payload["validator_source"],
            key="validator_source",
            root="validators",
            extensions=CPP_SOURCE_EXTENSIONS,
            label=label,
        )
    if "checker_source" in payload:
        result["checker_source"] = _source_path(
            payload["checker_source"],
            key="checker_source",
            root="checkers",
            extensions=CPP_SOURCE_EXTENSIONS,
            label=label,
        )
    if "interactor_source" in payload:
        result["interactor_source"] = _source_path(
            payload["interactor_source"],
            key="interactor_source",
            root="interactors",
            extensions=CPP_SOURCE_EXTENSIONS,
            label=label,
        )

    raw_sources = payload["generator_sources"]
    if not isinstance(raw_sources, list):
        raise ValueError(
            f"{label}.generator_sources: must be an array of source paths"
        )
    sources = [
        _source_path(
            item,
            key=f"generator_sources[{index}]",
            root="generators",
            extensions=SOLUTION_SOURCE_EXTENSIONS,
            label=label,
        )
        for index, item in enumerate(raw_sources)
    ]
    if len(set(sources)) != len(sources):
        raise ValueError(f"{label}.generator_sources: duplicate source path")
    result["generator_sources"] = sources

    result["generator_args"] = _string_array(
        payload["generator_args"], key="generator_args", label=label
    )
    result["validator_args"] = _string_array(
        payload["validator_args"], key="validator_args", label=label
    )
    result["checker_args"] = _string_array(
        payload["checker_args"], key="checker_args", label=label
    )

    result["generator_runs"] = _integer(
        payload["generator_runs"],
        key="generator_runs",
        minimum=0,
        maximum=4096,
        label=label,
    )
    result["compile_jobs"] = _integer(
        payload["compile_jobs"],
        key="compile_jobs",
        minimum=0,
        maximum=16,
        label=label,
    )
    result["validate_jobs"] = _integer(
        payload["validate_jobs"],
        key="validate_jobs",
        minimum=0,
        maximum=16,
        label=label,
    )
    result["solve_jobs"] = _integer(
        payload["solve_jobs"],
        key="solve_jobs",
        minimum=0,
        maximum=16,
        label=label,
    )
    result["run_jobs"] = _integer(
        payload["run_jobs"],
        key="run_jobs",
        minimum=0,
        maximum=16,
        label=label,
    )
    result["run_timeout_sec"] = _integer(
        payload["run_timeout_sec"],
        key="run_timeout_sec",
        minimum=1,
        maximum=300,
        label=label,
    )
    return result


def load_build_config(root: Path) -> BuildConfig:
    path = require_regular_source_file(root, BUILD_CONFIG_REL.as_posix())
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config/build.json: must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"config/build.json: cannot read file: {exc}") from exc
    return parse_build_config(text)


def ordered_build_config(payload: BuildConfig) -> dict[str, object]:
    return {key: payload[key] for key in BUILD_CONFIG_KEY_ORDER if key in payload}


def dumps_build_config(payload: BuildConfig) -> str:
    normalized = parse_build_config(
        json.dumps(dict(payload), ensure_ascii=False),
        label="config/build.json",
    )
    return json.dumps(
        ordered_build_config(normalized), ensure_ascii=False, indent=2
    ) + "\n"
