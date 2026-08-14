def _parse_int(raw: object) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, str, bytes, bytearray)):
        return int(raw)
    raise TypeError("value must be an integer")


def coerce_int(raw: object, default: int, min_value: int, max_value: int) -> int:
    try:
        value = _parse_int(raw)
    except (TypeError, ValueError):
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
        value = _parse_int(raw)
    except (TypeError, ValueError) as exc:
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
        value = _parse_int(raw)
    except (TypeError, ValueError):
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
    slack_ms = (
        wall_time_slack_sec_for_mode(
            mode,
            pass_limit=pass_limit,
            pass_fail_sec=pass_fail_slack_sec,
            multi_pass_sec=multi_pass_slack_sec,
            interactive_sec=interactive_slack_sec,
        )
        * 1000
    )
    return max(1, tl * 2 + slack_ms)
