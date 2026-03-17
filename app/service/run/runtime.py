from __future__ import annotations

import re


RUN_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in$")


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def normalize_problem_mode(raw: object, default: str = "pass-fail") -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return default
    if token in {"pass-fail", "interactive"}:
        return token
    raise ValueError(f"invalid problem mode: {token}")


def normalize_pass_limit(raw: object, default: int = 1) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except Exception as exc:
        raise ValueError("pass limit must be an integer") from exc
    return max(1, value)


def normalize_time_limit_ms(raw: object, *, default_ms: int, min_ms: int, max_ms: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = int(default_ms)
    return max(int(min_ms), min(int(max_ms), value))


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
    time_limit_ms: object,
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
    tl = normalize_time_limit_ms(
        time_limit_ms,
        default_ms=default_ms,
        min_ms=min_ms,
        max_ms=max_ms,
    )
    slack_ms = wall_time_slack_sec_for_mode(
        mode,
        pass_limit=pass_limit,
        pass_fail_sec=pass_fail_slack_sec,
        multi_pass_sec=multi_pass_slack_sec,
        interactive_sec=interactive_slack_sec,
    ) * 1000
    return max(1, tl * 2 + slack_ms)


def effective_run_timeout_sec(run_timeout_ms: object, *, max_timeout_sec: int) -> int:
    timeout_ms = max(1, int(run_timeout_ms))
    timeout_sec = max(1, (timeout_ms + 999) // 1000)
    return max(1, min(int(max_timeout_sec), timeout_sec))
