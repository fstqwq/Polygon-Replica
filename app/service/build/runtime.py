from __future__ import annotations


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def normalize_problem_mode(raw: object, default: str = "pass-fail") -> str:
    token = str(raw or "").strip().lower()
    if token in {"pass-fail", "interactive", "multi-pass"}:
        return token
    return default


def normalize_time_limit_ms(raw: object, *, default_ms: int, min_ms: int, max_ms: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = int(default_ms)
    return max(int(min_ms), min(int(max_ms), value))


def wall_time_slack_sec_for_mode(
    mode: object,
    *,
    pass_fail_sec: int,
    multi_pass_sec: int,
    interactive_sec: int,
) -> int:
    token = normalize_problem_mode(mode)
    if token == "interactive":
        return max(0, int(interactive_sec))
    if token == "multi-pass":
        return max(0, int(multi_pass_sec))
    return max(0, int(pass_fail_sec))


def effective_run_timeout_ms(
    time_limit_ms: int,
    *,
    mode: object,
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
        pass_fail_sec=pass_fail_slack_sec,
        multi_pass_sec=multi_pass_slack_sec,
        interactive_sec=interactive_slack_sec,
    ) * 1000
    return max(1, tl * 2 + slack_ms)


def effective_run_timeout_sec(run_timeout_ms: int) -> int:
    timeout_ms = max(1, int(run_timeout_ms))
    return max(1, (timeout_ms + 999) // 1000)


def load_problem_runtime_config(
    snapshot: object,
    *,
    default_time_limit_ms: int,
    default_mode: str,
    min_time_limit_ms: int,
    max_time_limit_ms: int,
) -> dict:
    from pathlib import Path
    import json

    cfg = {
        "time_limit_ms": int(default_time_limit_ms),
        "mode": str(default_mode or "pass-fail"),
    }
    path = Path(snapshot) / "config" / "problem.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cfg.update(payload)
        except Exception:
            pass
    cfg["time_limit_ms"] = normalize_time_limit_ms(
        cfg.get("time_limit_ms"),
        default_ms=default_time_limit_ms,
        min_ms=min_time_limit_ms,
        max_ms=max_time_limit_ms,
    )
    cfg["mode"] = normalize_problem_mode(cfg.get("mode"), default_mode)
    return cfg
