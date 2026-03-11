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
    token = str(raw or "").strip().lower()
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
            "source_hash": str(source_hash or "").strip().lower(),
            "compile_hash": str(compile_hash or "").strip().lower(),
            "run_hash": str(run_hash or "").strip().lower(),
            "compare_hash": str(compare_hash or "").strip().lower(),
            "compile_config_hash": str(compile_config_hash or "").strip().lower(),
            "run_config_hash": str(run_config_hash or "").strip().lower(),
            "compare_config_hash": str(compare_config_hash or "").strip().lower(),
            "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
        }
    )
    return (safe_testcase_hash, signature)


def domjudge_solve_output_cache_ref(
    *,
    source_hash: str,
    compile_hash: str,
    run_hash: str,
    compile_config_hash: str,
    run_config_hash: str,
    toolchain_cmd_digest: str,
    testcase_input_hash: str,
) -> tuple[str, str]:
    safe_input_hash = domjudge_safe_hash(testcase_input_hash)
    signature = JudgeFsIndexService.signature(
        {
            "schema": "solve-output-cache",
            "source_hash": str(source_hash or "").strip().lower(),
            "compile_hash": str(compile_hash or "").strip().lower(),
            "run_hash": str(run_hash or "").strip().lower(),
            "compile_config_hash": str(compile_config_hash or "").strip().lower(),
            "run_config_hash": str(run_config_hash or "").strip().lower(),
            "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
        }
    )
    return (safe_input_hash, signature)


def domjudge_cache_blob_ref(*, kind: str, key_hash: str, signature: str, name: str) -> str:
    safe_kind = str(kind or "").strip().lower()
    safe_key = str(key_hash or "").strip().lower()
    safe_sig = str(signature or "").strip().lower()
    safe_name = Path(str(name or "").strip()).name
    return f"cache://{safe_kind}/{safe_key}/{safe_sig}/{safe_name}"


def domjudge_parse_cache_blob_ref(token: str) -> tuple[str, str, str, str] | None:
    match = _DOMJUDGE_CACHE_BLOB_REF_RE.fullmatch(str(token or "").strip())
    if match is None:
        return None
    return (
        str(match.group("kind") or "").strip().lower(),
        str(match.group("key") or "").strip().lower(),
        str(match.group("sig") or "").strip().lower(),
        str(match.group("name") or "").strip(),
    )


def domjudge_json_hash(payload: object) -> str:
    encoded = canonical_json(payload, ensure_ascii=False)
    return sha256_hex_text(encoded)


def domjudge_source_hash(source_name: str, source_bytes: bytes) -> str:
    blob = bytes(source_bytes or b"")
    name = str(source_name or "").strip()
    payload = blob + b"\x00" + name.encode("utf-8", errors="replace")
    return sha256_hex_bytes(payload)


def domjudge_manifest_digest(rows: list[dict[str, object]]) -> str:
    canonical_rows = sorted(
        [
            {
                "path": str(item.get("path") or "").strip(),
                "blob_key": str(item.get("blob_key") or "").strip(),
            }
            for item in rows
            if isinstance(item, dict)
        ],
        key=lambda item: (item["path"], item["blob_key"]),
    )
    return sha256_hex_json(canonical_rows)


def domjudge_sha256_bytes(blob: bytes) -> str:
    raw = bytes(blob or b"")
    if not raw:
        return sha256_hex_bytes(b"")
    return sha256_hex_bytes(raw)


def domjudge_hash_of_hashes(hex_hashes: list[str]) -> str:
    normalized = [str(item or "").strip().lower() for item in hex_hashes]
    return sha256_hex_of_hashes(normalized)


def domjudge_set_hash_from_blobs(blobs: list[bytes]) -> str:
    hashed = [domjudge_sha256_bytes(blob) for blob in blobs]
    return domjudge_hash_of_hashes(hashed)


