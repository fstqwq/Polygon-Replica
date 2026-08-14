from collections.abc import Mapping
from pathlib import Path
from app.service.judgehost.domjudge.limits import (
    compile_output_kb,
    config_int,
    run_output_kb,
)
from app.service.judgehost.state import JudgehostHostRow


def decode_text(
    *,
    raw: object,
    default: str = "",
    lower: bool = False,
) -> str:
    """Decode one optional textual field at an untyped protocol boundary."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise RuntimeError("DOMjudge text field must be a string")
    text = raw.strip()
    if text:
        return text.lower() if lower else text
    return default


def decode_basename(*, raw: object, default: str = "") -> str:
    token = decode_text(raw=raw, default=default)
    if not token:
        return default
    return Path(token).name


def hosts_payload(hosts_state: dict[str, JudgehostHostRow]) -> list[dict[str, object]]:
    rows = sorted(
        (dict(row) for row in hosts_state.values()),
        key=lambda item: (
            decode_text(raw=item.get("last_seen_at")),
            decode_text(raw=item.get("hostname")),
        ),
        reverse=True,
    )
    out: list[dict[str, object]] = []
    for row in rows:
        token = decode_text(raw=row.get("hostname"))
        if not token:
            continue
        out.append(
            {
                "hostname": token,
                "enabled": bool(row.get("enabled", True)),
                "polltime": decode_text(raw=row.get("last_seen_at")),
            }
        )
    return out


def config_payload(values: Mapping[str, object]) -> dict[str, object]:
    compile_timeout = config_int(values, "TOOLCHAIN_COMPILE_TIMEOUT_SEC")
    compile_mem_mb = config_int(values, "TOOLCHAIN_COMPILE_MEMORY_MB")
    return {
        "diskspace_error": 1048576,
        # DOMjudge applies output_storage_limit to program stdout artifacts.
        # Generator stdout becomes verification input for downstream tasks, so
        # this must follow the run input/output cap, not the saved-log cap.
        "output_storage_limit": int(run_output_kb(values) * 1024),
        "script_timelimit": compile_timeout,
        "script_memory_limit": int(compile_mem_mb * 1024),
        "script_filesize_limit": int(compile_output_kb(values)),
        "timelimit_overshoot": "1s|100%",
    }


def languages_payload() -> list[dict[str, object]]:
    return [
        {"id": "c", "extensions": ["c"]},
        {"id": "cpp", "extensions": ["cpp", "cc", "cxx", "c++"]},
        {"id": "java", "extensions": ["java"]},
        {"id": "py", "extensions": ["py"]},
    ]
