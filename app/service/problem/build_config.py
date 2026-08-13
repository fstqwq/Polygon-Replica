"""Canonical ``config/build.json`` model and codec."""

from collections.abc import Collection
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
)
from app.service.problem.runtime_config import ProblemMode
from app.service.problem.source_file import require_regular_source_file

BUILD_CONFIG_REL = Path("config/build.json")
BUILD_CONFIG_KEY_ORDER: tuple[str, ...] = (
    "accepted_solution_source",
    "validator_source",
    "checker_source",
    "interactor_source",
    "generator_sources",
)
BUILD_CONFIG_KEYS = frozenset(BUILD_CONFIG_KEY_ORDER)
REMOVED_BUILD_CONFIG_KEYS = frozenset(
    {
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
    generator_sources: list[str]
    accepted_solution_source: NotRequired[str]
    validator_source: NotRequired[str]
    checker_source: NotRequired[str]
    interactor_source: NotRequired[str]


class AuthoringBuildConfig(TypedDict):
    config: BuildConfig
    removed_keys: tuple[str, ...]
    extra_fields: tuple[str, ...]
    error: str


def mode_extra_build_fields(
    problem_mode: ProblemMode | None,
) -> frozenset[str]:
    if problem_mode == "pass-fail":
        return frozenset({"interactor_source"})
    if problem_mode == "interactive":
        return frozenset({"checker_source"})
    return frozenset()


def validate_build_fields_for_mode(
    fields: Collection[str],
    *,
    problem_mode: ProblemMode | None,
    label: str,
) -> None:
    extra_fields = sorted(
        frozenset(fields).intersection(mode_extra_build_fields(problem_mode))
    )
    if not extra_fields:
        return
    assert problem_mode is not None
    raise ValueError(
        mode_extra_build_field_message(
            extra_fields[0],
            problem_mode=problem_mode,
            label=label,
        )
    )


def mode_extra_build_field_message(
    field: str,
    *,
    problem_mode: ProblemMode,
    label: str,
) -> str:
    article = "an" if problem_mode == "interactive" else "a"
    return (
        f"{label}: extra field '{field}' in {article} "
        f"{problem_mode} problem; remove it"
    )


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


def _generator_sources(raw: object, *, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{label}.generator_sources: must be an array")
    sources = [
        _source_path(
            item,
            key=f"generator_sources[{index}]",
            root="generators",
            extensions=SOLUTION_SOURCE_EXTENSIONS,
            label=label,
        )
        for index, item in enumerate(raw)
    ]
    if len(set(sources)) != len(sources):
        raise ValueError(f"{label}.generator_sources: duplicate source path")
    return sources


def parse_build_config(
    text: str,
    *,
    label: str = "config/build.json",
    problem_mode: ProblemMode | None = None,
) -> BuildConfig:
    payload = loads_object(text, label=label)
    reject_unknown_keys(payload, allowed=BUILD_CONFIG_KEYS, label=label)
    validate_build_fields_for_mode(
        payload,
        problem_mode=problem_mode,
        label=label,
    )
    result = BuildConfig(generator_sources=[])

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
    if "generator_sources" in payload:
        result["generator_sources"] = _generator_sources(
            payload["generator_sources"],
            label=label,
        )

    return result


def inspect_authoring_build_config(
    text: str,
    *,
    label: str = "config/build.json",
    problem_mode: ProblemMode | None = None,
) -> AuthoringBuildConfig:
    """Read current or safely-projectable authored configuration.

    Strict consumers use :func:`parse_build_config`. This operation exists for
    authoring pages, where known obsolete fields can be removed and fields
    that do not apply to the selected problem mode can be diagnosed without
    hiding the remaining valid selections. Unknown fields and invalid current
    values remain errors.
    """

    try:
        payload = loads_object(text, label=label)
    except ValueError as exc:
        return {
            "config": BuildConfig(generator_sources=[]),
            "removed_keys": (),
            "extra_fields": (),
            "error": str(exc),
        }

    unknown = frozenset(payload).difference(BUILD_CONFIG_KEYS)
    extra_keys = mode_extra_build_fields(problem_mode)
    extra_fields = tuple(sorted(frozenset(payload).intersection(extra_keys)))
    unsupported = sorted(unknown.difference(REMOVED_BUILD_CONFIG_KEYS))
    if unsupported:
        return {
            "config": BuildConfig(generator_sources=[]),
            "removed_keys": (),
            "extra_fields": extra_fields,
            "error": f"{label}: unsupported key '{unsupported[0]}'",
        }

    projected = {
        key: payload[key]
        for key in BUILD_CONFIG_KEY_ORDER
        if key in payload and key not in extra_keys
    }
    try:
        config = parse_build_config(
            json.dumps(projected, ensure_ascii=False),
            label=label,
            problem_mode=problem_mode,
        )
    except ValueError as exc:
        return {
            "config": BuildConfig(generator_sources=[]),
            "removed_keys": (),
            "extra_fields": extra_fields,
            "error": str(exc),
        }
    return {
        "config": config,
        "removed_keys": tuple(sorted(unknown)),
        "extra_fields": extra_fields,
        "error": "",
    }


def load_build_config(
    root: Path,
    *,
    problem_mode: ProblemMode | None = None,
) -> BuildConfig:
    path = require_regular_source_file(root, BUILD_CONFIG_REL.as_posix())
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config/build.json: must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"config/build.json: cannot read file: {exc}") from exc
    return parse_build_config(text, problem_mode=problem_mode)


def ordered_build_config(payload: BuildConfig) -> dict[str, object]:
    return {
        key: payload[key]
        for key in BUILD_CONFIG_KEY_ORDER
        if key in payload and (key != "generator_sources" or payload[key])
    }


def dumps_build_config(payload: BuildConfig) -> str:
    normalized = parse_build_config(
        json.dumps(dict(payload), ensure_ascii=False),
        label="config/build.json",
    )
    return json.dumps(
        ordered_build_config(normalized), ensure_ascii=False, indent=2
    ) + "\n"
