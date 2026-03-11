from __future__ import annotations

from pathlib import Path
from typing import Callable


def domjudge_read_artifact_blob(
    *,
    parse_cache_blob_ref: Callable[[str], tuple[str, str, str, str] | None],
    cache_read_blob: Callable[[str, str, str, str], bytes | None],
    work_root: Path,
    token: str,
) -> bytes | None:
    safe_token = str(token or "").strip()
    if not safe_token:
        return None
    parsed = parse_cache_blob_ref(safe_token)
    if parsed is not None:
        kind, key_hash, signature, name = parsed
        return cache_read_blob(kind, key_hash, signature, name)
    source = (work_root / safe_token).resolve()
    if source == work_root or work_root not in source.parents:
        return None
    if (not source.exists()) or (not source.is_file()) or source.is_symlink():
        return None
    try:
        return source.read_bytes()
    except OSError:
        return None


def resolve_artifact_blob(
    *,
    parse_cache_blob_ref: Callable[[str], tuple[str, str, str, str] | None],
    cache_read_blob: Callable[[str, str, str, str], bytes | None],
    read_artifact_blob: Callable[[Path, str], bytes | None],
    token: str,
    work_root: str | Path | None = None,
) -> bytes | None:
    safe_token = str(token or "").strip()
    if not safe_token:
        return None
    parsed = parse_cache_blob_ref(safe_token)
    if parsed is not None:
        kind, key_hash, signature, name = parsed
        return cache_read_blob(kind, key_hash, signature, name)
    if work_root is None:
        return None
    root = Path(str(work_root)).resolve()
    return read_artifact_blob(root, safe_token)
