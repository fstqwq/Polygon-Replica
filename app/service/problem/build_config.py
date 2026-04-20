from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

BUILD_CONFIG_KEY_ORDER: tuple[str, ...] = (
    "accepted_solution_source",
    "validator_source",
    "checker_source",
    "interactor_source",
    "generator_sources",
)


def ordered_build_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    ordered: dict[str, Any] = {}
    for key in BUILD_CONFIG_KEY_ORDER:
        if key in data:
            ordered[key] = data.pop(key)
    for key in sorted(data):
        ordered[key] = data[key]
    return ordered


def dumps_build_config(payload: Mapping[str, Any]) -> str:
    return json.dumps(ordered_build_config(payload), ensure_ascii=False, indent=2) + "\n"
