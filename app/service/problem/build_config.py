"""Canonical ``config/build.json`` model and codec."""

import json
from pathlib import Path, PurePosixPath
from typing import TypedDict

from app.main_constant import (
    CPP_SOURCE_EXTENSIONS,
    SOLUTION_SOURCE_EXTENSIONS,
)
from app.service.problem.json_codec import (
    loads_object,
    reject_unknown_keys,
)
from app.service.problem.source_file import require_regular_source_file

BUILD_CONFIG_REL = Path("config/build.json")
BUILD_CONFIG_KEY_ORDER: tuple[str, ...] = (
    "accepted_solution_source",
    "validator_source",
    "checker_source",
    "interactor_source",
)
BUILD_CONFIG_KEYS = frozenset(BUILD_CONFIG_KEY_ORDER)
REMOVED_BUILD_CONFIG_KEYS = frozenset(
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


class BuildConfig(TypedDict, total=False):
    accepted_solution_source: str
    validator_source: str
    checker_source: str
    interactor_source: str


class AuthoringBuildConfig(TypedDict):
    config: BuildConfig
    removed_keys: tuple[str, ...]
    error: str


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


def parse_build_config(
    text: str,
    *,
    label: str = "config/build.json",
) -> BuildConfig:
    payload = loads_object(text, label=label)
    reject_unknown_keys(payload, allowed=BUILD_CONFIG_KEYS, label=label)
    result = BuildConfig()

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

    return result


def inspect_authoring_build_config(
    text: str,
    *,
    label: str = "config/build.json",
) -> AuthoringBuildConfig:
    """Read current or safely-projectable authored configuration.

    Strict consumers use :func:`parse_build_config`. This operation exists for
    authoring pages, where known obsolete fields can be removed without
    guessing any current selection. Unknown fields and invalid current values
    remain errors.
    """

    try:
        payload = loads_object(text, label=label)
    except ValueError as exc:
        return {"config": BuildConfig(), "removed_keys": (), "error": str(exc)}

    unknown = frozenset(payload).difference(BUILD_CONFIG_KEYS)
    unsupported = sorted(unknown.difference(REMOVED_BUILD_CONFIG_KEYS))
    if unsupported:
        return {
            "config": BuildConfig(),
            "removed_keys": (),
            "error": f"{label}: unsupported key '{unsupported[0]}'",
        }

    projected = {
        key: payload[key]
        for key in BUILD_CONFIG_KEY_ORDER
        if key in payload
    }
    try:
        config = parse_build_config(
            json.dumps(projected, ensure_ascii=False),
            label=label,
        )
    except ValueError as exc:
        return {"config": BuildConfig(), "removed_keys": (), "error": str(exc)}
    return {
        "config": config,
        "removed_keys": tuple(sorted(unknown)),
        "error": "",
    }


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
