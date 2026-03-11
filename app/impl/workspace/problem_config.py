from __future__ import annotations

import json
from pathlib import Path

from app.impl.runtime.config import config
from app.main_util import safe_workspace_path

_C = config.constants


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def form_text(value: object) -> str:
    if isinstance(value, str):
        return value
    default = getattr(value, "default", "")
    if default is Ellipsis:
        return ""
    if isinstance(default, str):
        return default
    return str(default or "")


def normalize_problem_mode(raw: object, default: str = "pass-fail") -> str:
    token = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    if token in _C.GENERAL_MODE_VALUES:
        return token
    if default in _C.GENERAL_MODE_VALUES:
        return default
    return "pass-fail"


def sanitize_stdio_name(raw: str, fallback: str, label: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return fallback
    if len(value) > 128:
        raise ValueError(f"{label} is too long")
    if any((ch.isspace() for ch in value)):
        raise ValueError(f"{label} cannot contain spaces")
    if "/" in value or "\\" in value:
        raise ValueError(f"{label} cannot contain path separators")
    if value in {".", ".."}:
        raise ValueError(f"{label} is invalid")
    return value


def read_problem_config(workspace: Path) -> tuple[dict, dict, Path]:
    cfg_path = safe_workspace_path(workspace, str(_C.GENERAL_CONFIG_REL))
    payload: dict = {}
    if cfg_path.exists() and cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = dict(raw)
        except Exception:
            payload = {}
    mode = normalize_problem_mode(payload.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
    ui_cfg = {
        "input_file": sanitize_stdio_name(
            str(payload.get("input_file") or _C.GENERAL_CONFIG_DEFAULTS["input_file"]),
            str(_C.GENERAL_CONFIG_DEFAULTS["input_file"]),
            "input file",
        ),
        "output_file": sanitize_stdio_name(
            str(payload.get("output_file") or _C.GENERAL_CONFIG_DEFAULTS["output_file"]),
            str(_C.GENERAL_CONFIG_DEFAULTS["output_file"]),
            "output file",
        ),
        "time_limit_ms": coerce_int(
            payload.get("time_limit_ms"),
            int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            _C.GENERAL_TIME_LIMIT_MIN_MS,
            _C.GENERAL_TIME_LIMIT_MAX_MS,
        ),
        "memory_limit_mb": coerce_int(
            payload.get("memory_limit_mb"),
            int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
            _C.GENERAL_MEMORY_LIMIT_MIN_MB,
            _C.GENERAL_MEMORY_LIMIT_MAX_MB,
        ),
        "mode": mode,
    }
    return (payload, ui_cfg, cfg_path)




