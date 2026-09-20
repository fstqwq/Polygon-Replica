from collections.abc import Mapping

from app.config.model import ConfigValue
from app.service.platform.truncation import STORED_LOG_TRUNCATED_MARKER


def config_int(values: Mapping[str, ConfigValue], key: str) -> int:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"invalid internal integer configuration: {key}")
    return value


def upload_max_bytes(values: Mapping[str, ConfigValue]) -> int:
    return config_int(values, "UPLOAD_MAX_BYTES")


def run_output_kb(values: Mapping[str, ConfigValue]) -> int:
    return max(1, upload_max_bytes(values) // 1024)


def compile_output_kb(values: Mapping[str, ConfigValue]) -> int:
    return config_int(values, "TOOLCHAIN_COMPILE_OUTPUT_KB")


def run_memory_limit_kb(memory_limit_mb: object) -> int:
    if (
        isinstance(memory_limit_mb, bool)
        or not isinstance(memory_limit_mb, int)
        or memory_limit_mb < 1
    ):
        raise ValueError("invalid internal memory_limit_mb: expected an integer >= 1")
    return memory_limit_mb * 1024


def stored_log_limit_bytes(values: Mapping[str, ConfigValue]) -> int:
    return config_int(values, "JUDGEHOST_STORED_LOG_LIMIT_BYTES")


def judgehost_form_part_limit_bytes(
    values: Mapping[str, ConfigValue],
    *,
    headroom_bytes: int,
) -> int:
    raw_part_limit_bytes = upload_max_bytes(values)
    base64_part_limit_bytes = ((raw_part_limit_bytes + 2) // 3) * 4
    return base64_part_limit_bytes + int(headroom_bytes)


def truncate_stored_log_bytes(
    raw: bytes,
    values: Mapping[str, ConfigValue],
) -> bytes:
    # This limit is only for server-side auxiliary logs such as compile output
    # and compile metadata. Do not use it for program.out/output_run artifacts.
    limit = stored_log_limit_bytes(values)
    if len(raw) <= limit:
        return raw
    marker = STORED_LOG_TRUNCATED_MARKER
    if limit <= len(marker):
        return marker[:limit]
    return raw[: limit - len(marker)] + marker
