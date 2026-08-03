from __future__ import annotations

import re

from app.service.platform.hashing import sha256_hex_json


_DOMJUDGE_NUMERIC_ID_MODULUS = 1 << 63
_VERIFICATION_ID_RE = re.compile(r"^ver-([0-9a-f]+)$")
_COMPILE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_verification_id(raw: str) -> str:
    match = _VERIFICATION_ID_RE.fullmatch(raw)
    if match is None:
        raise RuntimeError("invalid verification id")
    return raw


def domjudge_job_id(verification_id: str) -> int:
    canonical = canonical_verification_id(verification_id)
    return int(canonical.removeprefix("ver-"), 16) % _DOMJUDGE_NUMERIC_ID_MODULUS


def compile_key(
    *,
    source_hash: str,
    compile_hash: str,
    compile_config: dict[str, object],
    entry_point: str | None,
    memory_limit: int,
) -> str:
    return sha256_hex_json(
        {
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "compile_config": compile_config,
            "entry_point": entry_point,
            "memory_limit": int(memory_limit),
        },
        ensure_ascii=True,
    )


def domjudge_submit_id(full_compile_key: str) -> int:
    if _COMPILE_KEY_RE.fullmatch(full_compile_key) is None:
        raise RuntimeError("invalid compile key")
    return int(full_compile_key, 16) % _DOMJUDGE_NUMERIC_ID_MODULUS
