import re

from app.service.platform.hashing import (
    md5_hex_bytes,
    md5_hex_text,
    sha256_hex_bytes,
    sha256_hex_of_hashes,
)
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


def executable_hash(files: list[tuple[str, bytes, bool]]) -> str:
    """Return the executable identity calculated by DOMjudge judgedaemon."""
    rows = sorted(files, key=lambda item: item[0])
    parts = [
        f"{md5_hex_bytes(content)}{filename}{'1' if is_executable else ''}"
        for filename, content, is_executable in rows
    ]
    return md5_hex_text("".join(parts), errors="replace")


def case_cache_ref(
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
    if re.fullmatch(r"[0-9a-f]{64}", testcase_hash) is None:
        raise RuntimeError("invalid canonical testcase hash")
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
    return (testcase_hash, signature)


def submission_source_hash(source_name: str, source_bytes: bytes) -> str:
    blob = bytes(source_bytes)
    name = source_name.strip()
    payload = blob + b"\x00" + name.encode("utf-8", errors="replace")
    return sha256_hex_bytes(payload)


def hash_of_hashes(hex_hashes: list[str]) -> str:
    normalized = [item.strip().lower() for item in hex_hashes]
    return sha256_hex_of_hashes(normalized)


def blob_set_hash(blobs: list[bytes]) -> str:
    return hash_of_hashes([sha256_hex_bytes(blob) for blob in blobs])
