from __future__ import annotations


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def normalize_problem_mode(raw: object, default: str = "pass-fail") -> str:
    token = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    if not token:
        return default
    if token in {"pass-fail", "interactive"}:
        return token
    raise ValueError(f"invalid problem mode: {token}")


def normalize_pass_limit(
    raw: object,
    default: int = 1,
    *,
    min_value: int = 1,
    max_value: int | None = None,
) -> int:
    if raw is None:
        base_value = int(default)
        if max_value is None:
            return max(int(min_value), base_value)
        return coerce_int(base_value, base_value, int(min_value), int(max_value))
    if isinstance(raw, str) and not raw.strip():
        base_value = int(default)
        if max_value is None:
            return max(int(min_value), base_value)
        return coerce_int(base_value, base_value, int(min_value), int(max_value))
    try:
        value = int(raw)
    except Exception as exc:
        raise ValueError("pass limit must be an integer") from exc
    safe_min = int(min_value)
    if max_value is None:
        return max(safe_min, value)
    safe_max = int(max_value)
    if value < safe_min or value > safe_max:
        raise ValueError(f"pass limit must be between {safe_min} and {safe_max}")
    return value


def normalize_time_limit_ms(raw: object, *, default_ms: int, min_ms: int, max_ms: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = int(default_ms)
    return max(int(min_ms), min(int(max_ms), value))


def normalize_memory_limit_mb(
    raw: object,
    *,
    default_mb: int,
    min_mb: int,
    max_mb: int,
) -> int:
    return coerce_int(raw, int(default_mb), int(min_mb), int(max_mb))


def wall_time_slack_sec_for_mode(
    mode: object,
    *,
    pass_limit: object,
    pass_fail_sec: int,
    multi_pass_sec: int,
    interactive_sec: int,
) -> int:
    token = normalize_problem_mode(mode)
    if token == "interactive":
        return max(0, int(interactive_sec))
    if normalize_pass_limit(pass_limit) > 1:
        return max(0, int(multi_pass_sec))
    return max(0, int(pass_fail_sec))


def effective_run_timeout_ms(
    time_limit_ms: int,
    *,
    mode: object,
    pass_limit: object,
    default_ms: int,
    min_ms: int,
    max_ms: int,
    pass_fail_slack_sec: int,
    multi_pass_slack_sec: int,
    interactive_slack_sec: int,
) -> int:
    tl = normalize_time_limit_ms(time_limit_ms, default_ms=default_ms, min_ms=min_ms, max_ms=max_ms)
    slack_ms = wall_time_slack_sec_for_mode(
        mode,
        pass_limit=pass_limit,
        pass_fail_sec=pass_fail_slack_sec,
        multi_pass_sec=multi_pass_slack_sec,
        interactive_sec=interactive_slack_sec,
    ) * 1000
    return max(1, tl * 2 + slack_ms)


def load_problem_runtime_config(
    snapshot: object,
    *,
    default_time_limit_ms: int,
    default_memory_limit_mb: int,
    default_mode: str,
    min_time_limit_ms: int,
    max_time_limit_ms: int,
    min_memory_limit_mb: int,
    max_memory_limit_mb: int,
) -> dict:
    from pathlib import Path
    import json

    cfg = {
        "time_limit_ms": int(default_time_limit_ms),
        "memory_limit_mb": int(default_memory_limit_mb),
        "mode": str(default_mode or "pass-fail"),
        "pass_limit": 1,
    }
    path = Path(snapshot) / "config" / "problem.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) or {}
            cfg.update(dict(payload))
        except Exception:
            pass
    cfg["time_limit_ms"] = normalize_time_limit_ms(
        cfg.get("time_limit_ms"),
        default_ms=default_time_limit_ms,
        min_ms=min_time_limit_ms,
        max_ms=max_time_limit_ms,
    )
    cfg["memory_limit_mb"] = normalize_memory_limit_mb(
        cfg.get("memory_limit_mb"),
        default_mb=default_memory_limit_mb,
        min_mb=min_memory_limit_mb,
        max_mb=max_memory_limit_mb,
    )
    cfg["mode"] = normalize_problem_mode(cfg.get("mode"), default_mode)
    cfg["pass_limit"] = normalize_pass_limit(cfg.get("pass_limit"), 1)
    return cfg
