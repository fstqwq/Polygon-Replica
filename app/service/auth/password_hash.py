import re

from app.service.platform.hashing import sha256_hex_text


_PASSWORD_VERIFIER_RE = re.compile(r"[0-9a-f]{64}")
_PASSWORD_VERIFIER_HASH_DOMAIN = "polygon-replica-password-verifier-v1|"


def password_verifier_storage_hash(verifier_hex: str) -> str:
    """Return the non-login-equivalent database hash for a password verifier."""

    safe_verifier = str(verifier_hex or "").strip().lower()
    if _PASSWORD_VERIFIER_RE.fullmatch(safe_verifier) is None:
        raise ValueError("invalid password verifier")
    return sha256_hex_text(f"{_PASSWORD_VERIFIER_HASH_DOMAIN}{safe_verifier}")
