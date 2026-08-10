from __future__ import annotations

from collections.abc import Mapping

from app.service.platform.truncation import STORED_LOG_TRUNCATED_MARKER

VERIFICATION_CASE_DISPATCH_BATCH_SIZE = 256


def run_output_kb(values: Mapping[str, object]) -> int:
    return int(values["RUN_EXEC_OUTPUT_KB"])


def compile_output_kb(values: Mapping[str, object]) -> int:
    return int(values["TOOLCHAIN_COMPILE_OUTPUT_KB"])


def run_memory_limit_kb(memory_limit_mb: object) -> int:
    if (
        isinstance(memory_limit_mb, bool)
        or not isinstance(memory_limit_mb, int)
        or memory_limit_mb < 1
    ):
        raise ValueError("invalid internal memory_limit_mb: expected an integer >= 1")
    return memory_limit_mb * 1024


def stored_log_limit_bytes(values: Mapping[str, object]) -> int:
    return int(values["JUDGEHOST_STORED_LOG_LIMIT_BYTES"])


def aux_display_limit_bytes(values: Mapping[str, object]) -> int:
    return int(values["AUX_DISPLAY_TEXT_LIMIT_BYTES"])


def judgehost_form_part_limit_bytes(
    values: Mapping[str, object],
    *,
    upload_max_bytes: int,
    default_part_limit_bytes: int,
    headroom_bytes: int,
) -> int:
    payload_limit_bytes = max(
        run_output_kb(values) * 1024,
        compile_output_kb(values) * 1024,
        stored_log_limit_bytes(values),
        aux_display_limit_bytes(values),
    )
    return max(
        int(default_part_limit_bytes),
        int(upload_max_bytes),
        int(payload_limit_bytes) + int(headroom_bytes),
    )


def truncate_stored_log_bytes(
    raw: bytes,
    values: Mapping[str, object],
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
