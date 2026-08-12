from typing import TypedDict


class ResourceLimitDisplay(TypedDict):
    time_limit_display: str
    time_limit_warn: bool
    memory_limit_display: str
    memory_limit_warn: bool


def resource_limit_display(time_limit_ms: int, memory_limit_mb: int) -> ResourceLimitDisplay:
    time_ms = int(time_limit_ms)
    memory_mb = int(memory_limit_mb)
    seconds = f"{time_ms / 1000:.3f}".rstrip("0").rstrip(".")
    memory = f"{memory_mb // 1024}G" if memory_mb % 1024 == 0 else f"{memory_mb}MB"
    return {
        "time_limit_display": f"{seconds}s",
        "time_limit_warn": time_ms < 500 or time_ms > 10_000,
        "memory_limit_display": memory,
        "memory_limit_warn": memory_mb < 256,
    }
