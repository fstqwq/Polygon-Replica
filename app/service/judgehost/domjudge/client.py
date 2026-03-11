from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


def domjudge_script_ids(job_id: int) -> tuple[int, int, int]:
    base = int(job_id) * 10
    return (base + 1, base + 2, base + 3)


def domjudge_parse_script_id(raw_id: object) -> tuple[int, int]:
    try:
        token = int(str(raw_id or "").strip())
    except Exception as exc:
        raise RuntimeError("invalid script id") from exc
    if token <= 0:
        raise RuntimeError("invalid script id")
    job_id = token // 10
    offset = token % 10
    if job_id <= 0 or offset not in {1, 2, 3}:
        raise RuntimeError("invalid script id")
    return (job_id, offset)


def domjudge_script_hash_field(kind: str) -> str:
    token = str(kind or "").strip().lower()
    mapping = {
        "compile": "compile_hash",
        "run": "run_hash",
        "compare": "compare_hash",
    }
    field = mapping.get(token)
    if not field:
        raise RuntimeError("invalid script kind")
    return field


def domjudge_script_dir_has_files(work_root: Path, kind: str) -> bool:
    try:
        base = (Path(work_root).resolve() / "scripts" / str(kind or "").strip().lower()).resolve()
    except Exception:
        return False
    if not base.exists() or not base.is_dir():
        return False
    try:
        for child in base.iterdir():
            if child.is_file():
                return True
    except Exception:
        return False
    return False


def domjudge_script_provider_job_id(
    *,
    kind: str,
    script_hash: str,
    default_job_id: int,
    fetch_rows: Callable[[str, str], list[Any]],
) -> int:
    safe_default = max(1, int(default_job_id))
    safe_hash = str(script_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", safe_hash):
        return safe_default
    try:
        field = domjudge_script_hash_field(kind)
    except Exception:
        return safe_default
    rows = fetch_rows(field, safe_hash)
    for row in rows:
        try:
            candidate_job_id = int(row["job_id"])
        except Exception:
            continue
        if candidate_job_id <= 0:
            continue
        work_root = Path(str(row["work_root"] or ""))
        if domjudge_script_dir_has_files(work_root, kind):
            return candidate_job_id
    return safe_default


