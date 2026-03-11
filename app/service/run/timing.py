from __future__ import annotations

from pathlib import Path


def cap_tle_time_ms(time_ms: int, timeout_ms: int) -> int:
    try:
        value = max(0, int(time_ms))
    except Exception:
        value = 0
    try:
        cap = max(1, int(timeout_ms))
    except Exception:
        cap = 0
    if cap > 0 and value > cap:
        return cap
    return value


def time_value_to_ms(token: object) -> int:
    raw = str(token or "").strip().replace(",", ".")
    if not raw:
        return 0
    try:
        return max(0, int(float(raw) * 1000.0))
    except Exception:
        return 0


def read_time_metrics(time_file: Path) -> tuple[int, int]:
    if not time_file.exists():
        return (0, 0)
    try:
        tokens = time_file.read_text(encoding="utf-8", errors="replace").strip().split()
    except OSError:
        return (0, 0)
    if not tokens:
        return (0, 0)
    mem_kb = 0
    user_ms = 0
    if len(tokens) >= 3 and str(tokens[0]).isdigit():
        mem_kb = int(tokens[0])
        user_ms = time_value_to_ms(tokens[1]) + time_value_to_ms(tokens[2])
        return (mem_kb, user_ms)
    if len(tokens) >= 2:
        user_ms = time_value_to_ms(tokens[0]) + time_value_to_ms(tokens[1])
        return (0, user_ms)
    if str(tokens[0]).isdigit():
        mem_kb = int(tokens[0])
    return (mem_kb, 0)


