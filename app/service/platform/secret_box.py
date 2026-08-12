"""Small authenticated encryption helper for database-stored secrets."""

import base64
import os
import secrets
from dataclasses import dataclass


ENCRYPTION_KEY_ENV = "POLYGON_REPLICA_ENCRYPTION_KEY"
_PREFIX = "enc:v1:aes-256-gcm"


class SecretBoxConfigError(ValueError):
    """Raised when the deployment encryption key is missing or invalid."""


class SecretBoxDecryptError(ValueError):
    """Raised when a stored encrypted secret cannot be decrypted."""


@dataclass(frozen=True)
class SecretBoxStatus:
    ready: bool
    label: str
    message: str


def _b64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    safe = token.strip()
    padding = "=" * ((4 - len(safe) % 4) % 4)
    return base64.urlsafe_b64decode((safe + padding).encode("ascii"))


def _load_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # pragma: no cover - exercised only with broken deployment deps.
        raise SecretBoxConfigError("cryptography dependency is not installed") from exc
    return AESGCM


class SecretBox:
    """AES-GCM envelope encryption using a deployment-provided key."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise SecretBoxConfigError(f"{ENCRYPTION_KEY_ENV} must decode to 32 bytes")
        self._key = bytes(key)

    @classmethod
    def from_environment(cls) -> "SecretBox":
        raw = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
        if not raw:
            raise SecretBoxConfigError(f"{ENCRYPTION_KEY_ENV} is not configured")
        try:
            key = _b64url_decode(raw)
        except Exception as exc:
            raise SecretBoxConfigError(f"{ENCRYPTION_KEY_ENV} must be base64url") from exc
        return cls(key)

    @classmethod
    def environment_status(cls) -> SecretBoxStatus:
        try:
            cls.from_environment()
            _load_aesgcm()
        except SecretBoxConfigError as exc:
            raw = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
            label = "invalid" if raw else "missing"
            return SecretBoxStatus(False, label, str(exc))
        return SecretBoxStatus(True, "configured", "encryption key configured")

    def encrypt_text(self, plaintext: str, *, aad: bytes) -> str:
        aesgcm = _load_aesgcm()(self._key)
        nonce = secrets.token_bytes(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return f"{_PREFIX}:{_b64url_encode(nonce)}:{_b64url_encode(ciphertext)}"

    def decrypt_text(self, ciphertext: str, *, aad: bytes) -> str:
        parts = ciphertext.strip().split(":")
        if len(parts) != 5 or ":".join(parts[:3]) != _PREFIX:
            raise SecretBoxDecryptError("encrypted secret has an unsupported format")
        try:
            nonce = _b64url_decode(parts[3])
            encrypted = _b64url_decode(parts[4])
            aesgcm = _load_aesgcm()(self._key)
            plaintext = aesgcm.decrypt(nonce, encrypted, aad)
        except SecretBoxConfigError:
            raise
        except Exception as exc:
            raise SecretBoxDecryptError("encrypted secret cannot be decrypted") from exc
        return plaintext.decode("utf-8")
