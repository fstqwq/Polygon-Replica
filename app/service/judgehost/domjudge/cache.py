from __future__ import annotations

import re
from pathlib import Path

from app.service.platform.hashing import (
    canonical_json,
    sha256_hex_bytes,
    sha256_hex_json,
    sha256_hex_text,
    sha256_hex_of_hashes,
)
from app.service.platform.judge_fs_index import JudgeFsIndexService

_DOMJUDGE_CACHE_BLOB_REF_RE = re.compile(
    r"^cache://(?P<kind>[a-z-]+)/(?P<key>[0-9a-f]{64})/(?P<sig>[0-9a-f]{64})/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)


def domjudge_safe_hash(raw: str) -> str:
    token = raw.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", token):
        return token
    return sha256_hex_text(token, errors="replace")


def domjudge_case_cache_ref(
    *,
    source_hash: str,
    compile_hash: str,
    run_hash: str,
    compare_hash: str,
    compile_config_hash: str,
    run_config_hash: str,
    compare_config_hash: str,
    toolchain_cmd_digest: str,
    testcase_hash: str,
) -> tuple[str, str]:
    safe_testcase_hash = domjudge_safe_hash(testcase_hash)
    signature = JudgeFsIndexService.signature(
        {
            "schema": "case-cache",
            "source_hash": source_hash.strip().lower(),
            "compile_hash": compile_hash.strip().lower(),
            "run_hash": run_hash.strip().lower(),
            "compare_hash": compare_hash.strip().lower(),
            "compile_config_hash": compile_config_hash.strip().lower(),
            "run_config_hash": run_config_hash.strip().lower(),
            "compare_config_hash": compare_config_hash.strip().lower(),
            "toolchain_cmd_digest": toolchain_cmd_digest.strip().lower(),
        }
    )
    return (safe_testcase_hash, signature)


def domjudge_cache_blob_ref(*, kind: str, key_hash: str, signature: str, name: str) -> str:
    safe_kind = kind.strip().lower()
    safe_key = key_hash.strip().lower()
    safe_sig = signature.strip().lower()
    safe_name = Path(name.strip()).name
    return f"cache://{safe_kind}/{safe_key}/{safe_sig}/{safe_name}"


def domjudge_parse_cache_blob_ref(token: str) -> tuple[str, str, str, str] | None:
    match = _DOMJUDGE_CACHE_BLOB_REF_RE.fullmatch(token.strip())
    if match is None:
        return None
    return (
        match.group("kind").strip().lower(),
        match.group("key").strip().lower(),
        match.group("sig").strip().lower(),
        match.group("name").strip(),
    )


def domjudge_json_hash(payload: object) -> str:
    return sha256_hex_text(canonical_json(payload, ensure_ascii=False))


def domjudge_source_hash(source_name: str, source_bytes: bytes) -> str:
    blob = bytes(source_bytes)
    name = source_name.strip()
    payload = blob + b"\x00" + name.encode("utf-8", errors="replace")
    return sha256_hex_bytes(payload)


def domjudge_manifest_digest(rows: list[dict[str, object]]) -> str:
    canonical_rows = sorted(
        [
            {
                "path": item["path"].strip(),
                "blob_key": item["blob_key"].strip(),
            }
            for item in rows
        ],
        key=lambda item: (item["path"], item["blob_key"]),
    )
    return sha256_hex_json(canonical_rows)


def domjudge_sha256_bytes(blob: bytes) -> str:
    raw = bytes(blob)
    if not raw:
        return sha256_hex_bytes(b"")
    return sha256_hex_bytes(raw)


def domjudge_hash_of_hashes(hex_hashes: list[str]) -> str:
    normalized = [item.strip().lower() for item in hex_hashes]
    return sha256_hex_of_hashes(normalized)


def domjudge_set_hash_from_blobs(blobs: list[bytes]) -> str:
    hashed = [domjudge_sha256_bytes(blob) for blob in blobs]
    return domjudge_hash_of_hashes(hashed)


