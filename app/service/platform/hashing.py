import hashlib
import hmac
import json
from pathlib import Path
from typing import Iterable


def canonical_json(payload: object, *, ensure_ascii: bool = False) -> str:
    return json.dumps(payload, ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":"))


def canonical_json_bytes(payload: object, *, ensure_ascii: bool = False) -> bytes:
    return canonical_json(payload, ensure_ascii=ensure_ascii).encode("utf-8")


def sha256_hex_bytes(payload: bytes) -> str:
    return hashlib.sha256(bytes(payload or b"")).hexdigest()


def sha256_hex_text(text: str, *, errors: str = "strict") -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors=errors)).hexdigest()


def sha256_hex_json(payload: object, *, ensure_ascii: bool = False) -> str:
    return sha256_hex_bytes(canonical_json_bytes(payload, ensure_ascii=ensure_ascii))

def quick_fp_digest(entries: list[dict[str, object]], *, schema: str = "quick-fp") -> str:
    payload = {"schema": str(schema or "quick-fp"), "entries": list(entries or [])}
    return sha256_hex_json(payload, ensure_ascii=False)


def compile_command_digest(command: str, flags: list[str] | tuple[str, ...] | None = None) -> str:
    cmd = " ".join(str(command or "").split())
    flag_tokens = [" ".join(str(token or "").split()) for token in list(flags or []) if str(token or "").strip()]
    payload_obj = {"command": cmd, "flags": flag_tokens}
    return sha256_hex_json(payload_obj, ensure_ascii=False)


def sha256_hex_of_hashes(hex_hashes: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for token in hex_hashes:
        digest.update(bytes.fromhex(str(token or "").strip().lower()))
    return digest.hexdigest()


def md5_hex_bytes(payload: bytes) -> str:
    return hashlib.md5(bytes(payload or b"")).hexdigest()


def md5_hex_text(text: str, *, errors: str = "strict") -> str:
    return hashlib.md5(str(text or "").encode("utf-8", errors=errors)).hexdigest()


def hmac_sha256_hex(secret: bytes, payload: bytes) -> str:
    return hmac.new(bytes(secret or b""), bytes(payload or b""), hashlib.sha256).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

