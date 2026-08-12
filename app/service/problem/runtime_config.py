"""Canonical ``config/problem.json`` model and codec."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

from app.main_constant import GENERAL_CONFIG_DEFAULTS
from app.service.problem.json_codec import (
    loads_object,
    reject_unknown_keys,
    require_keys,
)
from app.service.problem.source_file import require_regular_source_file

ProblemMode = Literal["pass-fail", "interactive"]

PROBLEM_CONFIG_REL = Path("config/problem.json")
PROBLEM_CONFIG_KEYS = frozenset(
    {"time_limit_ms", "memory_limit_mb", "mode", "pass_limit"}
)


class ProblemConfig(TypedDict):
    time_limit_ms: int
    memory_limit_mb: int
    mode: ProblemMode
    pass_limit: int


@dataclass(frozen=True)
class ProblemConfigLimits:
    min_time_limit_ms: int
    max_time_limit_ms: int
    min_memory_limit_mb: int
    max_memory_limit_mb: int
    min_pass_limit: int
    max_pass_limit: int

    def __post_init__(self) -> None:
        _validate_bounds(
            "time_limit_ms",
            self.min_time_limit_ms,
            self.max_time_limit_ms,
        )
        _validate_bounds(
            "memory_limit_mb", self.min_memory_limit_mb, self.max_memory_limit_mb
        )
        _validate_bounds("pass_limit", self.min_pass_limit, self.max_pass_limit)


class ProblemConfigLimitSource(Protocol):
    GENERAL_TIME_LIMIT_MIN_MS: int
    GENERAL_TIME_LIMIT_MAX_MS: int
    GENERAL_MEMORY_LIMIT_MIN_MB: int
    GENERAL_MEMORY_LIMIT_MAX_MB: int
    GENERAL_PASS_LIMIT_MIN: int
    GENERAL_PASS_LIMIT_MAX: int


def _validate_bounds(label: str, minimum: int, maximum: int) -> None:
    if isinstance(minimum, bool) or isinstance(maximum, bool):
        raise ValueError(f"{label} bounds must be integers")
    if minimum > maximum:
        raise ValueError(f"{label} bounds are invalid")


def problem_config_limits(source: ProblemConfigLimitSource) -> ProblemConfigLimits:
    return ProblemConfigLimits(
        min_time_limit_ms=int(source.GENERAL_TIME_LIMIT_MIN_MS),
        max_time_limit_ms=int(source.GENERAL_TIME_LIMIT_MAX_MS),
        min_memory_limit_mb=int(source.GENERAL_MEMORY_LIMIT_MIN_MB),
        max_memory_limit_mb=int(source.GENERAL_MEMORY_LIMIT_MAX_MB),
        min_pass_limit=int(source.GENERAL_PASS_LIMIT_MIN),
        max_pass_limit=int(source.GENERAL_PASS_LIMIT_MAX),
    )


def default_problem_config(*, limits: ProblemConfigLimits) -> ProblemConfig:
    """Construct the canonical source written for a newly-created problem."""

    return {
        "time_limit_ms": min(
            limits.max_time_limit_ms,
            max(
                limits.min_time_limit_ms,
                int(GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            ),
        ),
        "memory_limit_mb": min(
            limits.max_memory_limit_mb,
            max(
                limits.min_memory_limit_mb,
                int(GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
            ),
        ),
        "mode": cast(ProblemMode, GENERAL_CONFIG_DEFAULTS["mode"]),
        "pass_limit": min(
            limits.max_pass_limit,
            max(
                limits.min_pass_limit,
                int(GENERAL_CONFIG_DEFAULTS["pass_limit"]),
            ),
        ),
    }


def _integer_field(
    payload: dict[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}.{key}: must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{label}.{key}: must be between {minimum} and {maximum}"
        )
    return value


def parse_problem_config(
    text: str,
    *,
    limits: ProblemConfigLimits,
    label: str = "config/problem.json",
) -> ProblemConfig:
    payload = loads_object(text, label=label)
    reject_unknown_keys(payload, allowed=PROBLEM_CONFIG_KEYS, label=label)
    require_keys(payload, required=PROBLEM_CONFIG_KEYS, label=label)

    mode = payload["mode"]
    if not isinstance(mode, str) or mode not in {"pass-fail", "interactive"}:
        raise ValueError(f"{label}.mode: must be 'pass-fail' or 'interactive'")

    return {
        "time_limit_ms": _integer_field(
            payload,
            "time_limit_ms",
            minimum=limits.min_time_limit_ms,
            maximum=limits.max_time_limit_ms,
            label=label,
        ),
        "memory_limit_mb": _integer_field(
            payload,
            "memory_limit_mb",
            minimum=limits.min_memory_limit_mb,
            maximum=limits.max_memory_limit_mb,
            label=label,
        ),
        "mode": cast(ProblemMode, mode),
        "pass_limit": _integer_field(
            payload,
            "pass_limit",
            minimum=limits.min_pass_limit,
            maximum=limits.max_pass_limit,
            label=label,
        ),
    }


def load_problem_config(
    root: Path,
    *,
    limits: ProblemConfigLimits,
) -> ProblemConfig:
    path = require_regular_source_file(root, PROBLEM_CONFIG_REL.as_posix())
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("config/problem.json: must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"config/problem.json: cannot read file: {exc}") from exc
    return parse_problem_config(text, limits=limits)


def dumps_problem_config(
    config: ProblemConfig,
    *,
    limits: ProblemConfigLimits,
) -> str:
    payload: dict[str, object] = {
        "time_limit_ms": config["time_limit_ms"],
        "memory_limit_mb": config["memory_limit_mb"],
        "mode": config["mode"],
        "pass_limit": config["pass_limit"],
    }
    normalized = parse_problem_config(
        json.dumps(payload, ensure_ascii=False),
        limits=limits,
    )
    return json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
