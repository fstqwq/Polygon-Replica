from __future__ import annotations


STORED_LOG_TRUNCATED_MARKER = b"\n...[truncated]\n"


def _constant_int(constants: object, key: str, *, default: int, minimum: int) -> int:
    try:
        value = int(getattr(constants, key, default) or default)
    except Exception:
        value = default
    return max(int(minimum), int(value))


def run_output_kb(constants: object) -> int:
    return _constant_int(constants, "RUN_EXEC_OUTPUT_KB", default=65536, minimum=64)


def compile_output_kb(constants: object) -> int:
    return _constant_int(constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", default=262144, minimum=1024)


def run_memory_limit_kb(memory_limit_mb: object) -> int:
    if (
        isinstance(memory_limit_mb, bool)
        or not isinstance(memory_limit_mb, int)
        or memory_limit_mb < 1
    ):
        raise ValueError("invalid internal memory_limit_mb: expected an integer >= 1")
    return memory_limit_mb * 1024


def stored_log_limit_bytes(constants: object) -> int:
    return _constant_int(constants, "JUDGEHOST_STORED_LOG_LIMIT_BYTES", default=65536, minimum=1024)


def aux_display_limit_bytes(constants: object) -> int:
    return _constant_int(constants, "AUX_DISPLAY_TEXT_LIMIT_BYTES", default=2048, minimum=256)


def judgehost_form_part_limit_bytes(
    constants: object,
    *,
    upload_max_bytes: int,
    default_part_limit_bytes: int,
    headroom_bytes: int,
) -> int:
    payload_limit_bytes = max(
        run_output_kb(constants) * 1024,
        compile_output_kb(constants) * 1024,
        stored_log_limit_bytes(constants),
        aux_display_limit_bytes(constants),
    )
    return max(
        int(default_part_limit_bytes),
        int(upload_max_bytes),
        int(payload_limit_bytes) + int(headroom_bytes),
    )


def truncate_stored_log_bytes(raw: bytes, constants: object) -> bytes:
    # This limit is only for server-side auxiliary logs such as compile output
    # and compile metadata. Do not use it for program.out/output_run artifacts.
    limit = stored_log_limit_bytes(constants)
    if len(raw) <= limit:
        return raw
    marker = STORED_LOG_TRUNCATED_MARKER
    if limit <= len(marker):
        return marker[:limit]
    return raw[: limit - len(marker)] + marker
