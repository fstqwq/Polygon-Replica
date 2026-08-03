from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from app.service.judgehost.limits import compile_output_kb, run_output_kb

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DOMJUDGE_CONTEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMJUDGE_PROTOCOL_TRACE_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_DOMJUDGE_PROTOCOL_TRACE_BYTES_RE = re.compile(rb"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_DOMJUDGE_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def domjudge_text(raw: object, *, default: str = "") -> str:
    text = cast(str | None, raw)
    if text is None:
        return default
    text = text.strip()
    if text:
        return text
    return default


def domjudge_lower_text(raw: object, *, default: str = "") -> str:
    return domjudge_text(raw, default=default).lower()


def domjudge_path_name(raw: object, *, default: str = "") -> str:
    token = domjudge_text(raw, default=default)
    if not token:
        return default
    return Path(token).name


def domjudge_task_lease_owner(task_row: dict[str, object] | None, *, default: str = "judgehost") -> str:
    if task_row is None:
        return default
    token = domjudge_text(task_row.get("lease_owner"))
    if token:
        return token
    return default


def domjudge_hosts_payload(hosts_state: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    rows = sorted(
        (dict(row) for row in hosts_state.values()),
        key=lambda item: (domjudge_text(item.get("last_seen_at")), domjudge_text(item.get("hostname"))),
        reverse=True,
    )
    out: list[dict[str, object]] = []
    for row in rows:
        token = domjudge_text(row.get("hostname"))
        if not token:
            continue
        out.append(
            {
                "hostname": token,
                "enabled": bool(row.get("enabled", True)),
                "polltime": domjudge_text(row.get("last_seen_at")),
            }
        )
    return out


def domjudge_config_from_constants(constants: object) -> dict[str, object]:
    compile_timeout = max(1, int(getattr(constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
    compile_mem_mb = max(64, int(getattr(constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
    return {
        "diskspace_error": 1048576,
        # DOMjudge applies output_storage_limit to program stdout artifacts.
        # Generator stdout becomes verification input for downstream tasks, so
        # this must follow the run input/output cap, not the saved-log cap.
        "output_storage_limit": int(run_output_kb(constants) * 1024),
        "script_timelimit": compile_timeout,
        "script_memory_limit": int(compile_mem_mb * 1024),
        "script_filesize_limit": int(compile_output_kb(constants)),
        "timelimit_overshoot": "1s|100%",
    }


def domjudge_languages_payload() -> list[dict[str, object]]:
    return [
        {"id": "c", "extensions": ["c"]},
        {"id": "cpp", "extensions": ["cpp", "cc", "cxx", "c++"]},
        {"id": "java", "extensions": ["java"]},
        {"id": "py", "extensions": ["py"]},
    ]
