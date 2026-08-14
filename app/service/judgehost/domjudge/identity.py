import re

from app.service.platform.hashing import sha256_hex_json

_DOMJUDGE_NUMERIC_ID_MODULUS = 1 << 63
_SCRIPT_ID_MODULUS = 1048576
_COMPILE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFICATION_WIRE_ID_RE = re.compile(r"^ver-([0-9a-f]+)$")


def job_id(verification_id: str) -> int:
    match = _VERIFICATION_WIRE_ID_RE.fullmatch(verification_id)
    if match is None:
        raise RuntimeError("invalid verification id")
    numeric_id = int(match.group(1), 16)
    if (
        numeric_id <= 0
        or numeric_id >= _DOMJUDGE_NUMERIC_ID_MODULUS
        or verification_id != f"ver-{numeric_id:x}"
    ):
        raise RuntimeError("invalid verification id")
    return numeric_id


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


def submit_id(full_compile_key: str) -> int:
    if _COMPILE_KEY_RE.fullmatch(full_compile_key) is None:
        raise RuntimeError("invalid compile key")
    return int(full_compile_key, 16) % _DOMJUDGE_NUMERIC_ID_MODULUS


def script_id(script_hash: str) -> int:
    if len(script_hash) != 32 or any(
        character not in "0123456789abcdef" for character in script_hash
    ):
        raise RuntimeError("invalid script hash")
    return (int(script_hash, 16) % _SCRIPT_ID_MODULUS) + 1


def parse_script_id(raw_id: object) -> int:
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        value = raw_id
    elif isinstance(raw_id, str):
        try:
            value = int(raw_id)
        except ValueError as exc:
            raise RuntimeError("invalid script id") from exc
    else:
        raise RuntimeError("invalid script id")
    if value <= 0:
        raise RuntimeError("invalid script id")
    return value


def script_hash_field(kind: str) -> str:
    fields = {
        "compile": "compile_hash",
        "run": "run_hash",
        "compare": "compare_hash",
    }
    try:
        return fields[kind]
    except KeyError as exc:
        raise RuntimeError("invalid script kind") from exc
