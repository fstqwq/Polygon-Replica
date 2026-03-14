from __future__ import annotations

import base64  
import json  
import logging
import re
import secrets  
import shlex  
import sqlite3  
import threading  
import time  
import uuid  
from contextlib import contextmanager  
from datetime import datetime, timezone  
from pathlib import Path  

from app.db import DB, now_iso  
from app.runtime_value import RuntimeValues  
from app.service.platform.hashing import (
    canonical_json,
    compile_command_digest,
    domjudge_executable_hash,
    sha256_hex_bytes,
    sha256_hex_json,
    sha256_hex_text,
    sha256_hex_of_hashes,
)
from app.service.platform.judge_fs_index import JudgeFsIndexService  
from app.service.run.runtime import RUN_TEST_NAME_RE  
from app.setting import Settings  

from ..artifact import domjudge_read_artifact_blob, resolve_artifact_blob  
from ..domdb import (
    domjudge_active_job_for_host,
    domjudge_cases_for_job,
    domjudge_shared_pending_job,
    is_domjudge_sql,
)  
from ..domjudge.cache import (
    domjudge_cache_blob_ref,
    domjudge_case_cache_ref,
    domjudge_hash_of_hashes,
    domjudge_json_hash,
    domjudge_manifest_digest,
    domjudge_parse_cache_blob_ref,
    domjudge_safe_hash,
    domjudge_set_hash_from_blobs,
    domjudge_sha256_bytes,
    domjudge_solve_output_cache_ref,
    domjudge_source_hash,
)  
from ..domjudge.client import (
    domjudge_parse_script_id,
    domjudge_script_dir_has_files,
    domjudge_script_hash_field,
    domjudge_script_ids,
    domjudge_script_provider_job_id,
)  
from ..progress import domjudge_solve_main_progress, domjudge_case_progress_for_runs  
from ..runtime import (
    domjudge_bool,
    domjudge_feedback_line_from_bytes,
    domjudge_feedback_line_from_text,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_rewrite_untrusted_runresult,
    domjudge_run_time_limit_sec,
    now_iso_after,
    parse_iso_utc,
)  


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VERIFICATION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_DOMJUDGE_SUBMIT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMJUDGE_CONTEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DOMJUDGE_PROTOCOL_TRACE_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_DOMJUDGE_PROTOCOL_TRACE_BYTES_RE = re.compile(rb"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_DOMJUDGE_CACHE_BLOB_REF_RE = re.compile(
    r"^cache://(?P<kind>[a-z-]+)/(?P<key>[0-9a-f]{64})/(?P<sig>[0-9a-f]{64})/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)
_DOMJUDGE_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

logger = logging.getLogger(__name__)


def domjudge_text(raw: object, *, default: str = "") -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise RuntimeError("DOMjudge text field must be str")
    text = raw.strip()
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
    compile_output_kb = max(64, int(getattr(constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
    output_kb = max(64, int(getattr(constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
    return {
        "diskspace_error": 1048576,
        "output_storage_limit": int(output_kb * 1024),
        "script_timelimit": compile_timeout,
        "script_memory_limit": int(compile_mem_mb * 1024),
        "script_filesize_limit": int(compile_output_kb),
        "timelimit_overshoot": "1s|100%",
    }


def domjudge_languages_payload() -> list[dict[str, object]]:
    return [
        {"id": "c", "extensions": ["c"]},
        {"id": "cpp", "extensions": ["cpp", "cc", "cxx", "c++"]},
        {"id": "java", "extensions": ["java"]},
        {"id": "py", "extensions": ["py"]},
    ]


def task_status_counts(
    task_rows: dict[str, dict[str, object]],
    *,
    queued: str,
    leased: str,
    completed: str,
    failed: str,
) -> dict[str, int]:
    out = {
        str(queued): 0,
        str(leased): 0,
        str(completed): 0,
        str(failed): 0,
    }
    for row in task_rows.values():
        token = domjudge_lower_text(row.get("status"))
        if token in out:
            out[token] = int(out[token]) + 1
    return out


