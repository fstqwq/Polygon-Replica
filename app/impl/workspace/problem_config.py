from __future__ import annotations

import app.main_constant as _K

import json
from pathlib import Path
from typing import cast

from app.impl.runtime.config import config
from app.main_util import safe_workspace_path
from app.service.verification.runtime import (
    coerce_int,
    normalize_memory_limit_mb,
    normalize_pass_limit,
    normalize_problem_mode,
)

_C = config.config_values


def read_problem_config(workspace: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    cfg_path = safe_workspace_path(workspace, str(_K.GENERAL_CONFIG_REL))
    payload: dict[str, object] = {}
    if cfg_path.exists() and cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("invalid problem.json") from exc
        if not isinstance(loaded, dict):
            raise ValueError("problem.json must be an object")
        payload = cast(dict[str, object], loaded)
        if "mode" not in payload:
            raise ValueError("problem.json missing mode")
        if "pass_limit" not in payload:
            raise ValueError("problem.json missing pass_limit")
    mode = normalize_problem_mode(payload.get("mode"), str(_K.GENERAL_CONFIG_DEFAULTS["mode"]))
    ui_cfg = {
        "time_limit_ms": coerce_int(
            payload.get("time_limit_ms"),
            int(_K.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            _C.GENERAL_TIME_LIMIT_MIN_MS,
            _C.GENERAL_TIME_LIMIT_MAX_MS,
        ),
        "memory_limit_mb": normalize_memory_limit_mb(
            payload.get("memory_limit_mb"),
            default_mb=int(_K.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
            min_mb=_C.GENERAL_MEMORY_LIMIT_MIN_MB,
            max_mb=_C.GENERAL_MEMORY_LIMIT_MAX_MB,
        ),
        "mode": mode,
        "pass_limit": normalize_pass_limit(
            payload.get("pass_limit"),
            int(_K.GENERAL_CONFIG_DEFAULTS["pass_limit"]),
            min_value=_C.GENERAL_PASS_LIMIT_MIN,
            max_value=_C.GENERAL_PASS_LIMIT_MAX,
        ),
    }
    return (payload, ui_cfg, cfg_path)
