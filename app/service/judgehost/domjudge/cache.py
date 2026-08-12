import re
from app.service.platform.hashing import (
    canonical_json,
    sha256_hex_bytes,
    sha256_hex_text,
    sha256_hex_of_hashes,
)
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


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
    signature = RuntimeCacheIndex.signature(
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


def domjudge_json_hash(payload: object) -> str:
    return sha256_hex_text(canonical_json(payload, ensure_ascii=False))


def domjudge_source_hash(source_name: str, source_bytes: bytes) -> str:
    blob = bytes(source_bytes)
    name = source_name.strip()
    payload = blob + b"\x00" + name.encode("utf-8", errors="replace")
    return sha256_hex_bytes(payload)


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
