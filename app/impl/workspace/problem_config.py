from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.impl.runtime.config import config
from app.main_util import safe_workspace_path

_C = config.constants


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def form_text(value: str | object) -> str:
    default = getattr(value, "default", value)
    if default is Ellipsis:
        return ""
    if default is None:
        return ""
    return str(default)


def normalize_problem_mode(raw: str | None, default: str = "pass-fail") -> str:
    if raw is None:
        token = ""
    else:
        token = raw.strip().lower().replace("_", "-").replace(" ", "-")
    if not token:
        if default in _C.GENERAL_MODE_VALUES:
            return default
        return "pass-fail"
    if token in _C.GENERAL_MODE_VALUES:
        return token
    raise ValueError(f"invalid problem mode: {token}")


def normalize_pass_limit(raw: object, default: int = 1) -> int:
    if raw is None:
        return coerce_int(default, default, _C.GENERAL_PASS_LIMIT_MIN, _C.GENERAL_PASS_LIMIT_MAX)
    text = str(raw).strip()
    if not text:
        return coerce_int(default, default, _C.GENERAL_PASS_LIMIT_MIN, _C.GENERAL_PASS_LIMIT_MAX)
    try:
        value = int(text)
    except Exception as exc:
        raise ValueError("pass limit must be an integer") from exc
    if value < _C.GENERAL_PASS_LIMIT_MIN or value > _C.GENERAL_PASS_LIMIT_MAX:
        raise ValueError(
            f"pass limit must be between {_C.GENERAL_PASS_LIMIT_MIN} and {_C.GENERAL_PASS_LIMIT_MAX}"
        )
    return value


def sanitize_stdio_name(raw: str, fallback: str, label: str) -> str:
    value = raw.strip()
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


def read_problem_config(workspace: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    cfg_path = safe_workspace_path(workspace, str(_C.GENERAL_CONFIG_REL))
    payload: dict[str, object] = {}
    if cfg_path.exists() and cfg_path.is_file():
        try:
            payload = cast(dict[str, object], json.loads(cfg_path.read_text(encoding="utf-8")))
        except Exception:
            payload = {}
    mode = normalize_problem_mode(payload.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
    input_file = cast(str | None, payload.get("input_file"))
    if input_file is None:
        input_file = str(_C.GENERAL_CONFIG_DEFAULTS["input_file"])
    output_file = cast(str | None, payload.get("output_file"))
    if output_file is None:
        output_file = str(_C.GENERAL_CONFIG_DEFAULTS["output_file"])
    ui_cfg = {
        "input_file": sanitize_stdio_name(
            input_file,
            str(_C.GENERAL_CONFIG_DEFAULTS["input_file"]),
            "input file",
        ),
        "output_file": sanitize_stdio_name(
            output_file,
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
        "pass_limit": normalize_pass_limit(
            payload.get("pass_limit"),
            int(_C.GENERAL_CONFIG_DEFAULTS["pass_limit"]),
        ),
    }
    return (payload, ui_cfg, cfg_path)




