import threading
from collections import OrderedDict
from pathlib import Path
from typing import TypedDict

from app.service.verification.signature import verification_fingerprint, verification_manifest
from app.service.verification.types import WorkspaceVerificationRow


_FINGERPRINT_CACHE_MAX = 1024


class _FingerprintCacheEntry(TypedDict):
    verification_id: str
    signature: str


_FINGERPRINT_CACHE: OrderedDict[
    tuple[int, int, str],
    _FingerprintCacheEntry,
] = OrderedDict()
_FINGERPRINT_CACHE_LOCK = threading.RLock()


def verification_sources_signature(workspace: Path) -> str:
    return verification_manifest(workspace).signature


def verification_sources_fingerprint(workspace: Path) -> str:
    return verification_fingerprint(workspace)


def remember_verification_fingerprint(
    problem_id: int,
    workspace_id: int,
    fingerprint: str,
    verification_id: str,
    signature: str = "",
) -> None:
    if not fingerprint or not verification_id:
        return
    key = (int(problem_id), int(workspace_id), fingerprint)
    with _FINGERPRINT_CACHE_LOCK:
        _FINGERPRINT_CACHE[key] = {
            "verification_id": verification_id,
            "signature": signature,
        }
        _FINGERPRINT_CACHE.move_to_end(key)
        while len(_FINGERPRINT_CACHE) > _FINGERPRINT_CACHE_MAX:
            _FINGERPRINT_CACHE.popitem(last=False)


def cached_verification_for_fingerprint(
    problem_id: int,
    workspace_id: int,
    fingerprint: str,
    rows: list[WorkspaceVerificationRow],
) -> tuple[WorkspaceVerificationRow | None, str]:
    if not fingerprint:
        return (None, "")
    key = (int(problem_id), int(workspace_id), fingerprint)
    by_id = {row["id"]: row for row in rows}
    with _FINGERPRINT_CACHE_LOCK:
        entry = _FINGERPRINT_CACHE.get(key)
        if entry is None:
            return (None, "")
        _FINGERPRINT_CACHE.move_to_end(key)
        row = by_id.get(entry["verification_id"])
        if row is None:
            _FINGERPRINT_CACHE.pop(key, None)
            return (None, "")
        return (row, entry["signature"])
