from __future__ import annotations

import base64
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.impl.auth.csrf import verify_password_form_csrf_token
from app.impl.auth.shared import normalize_password_verifier_hex
from app.impl.runtime.config import config
from app.service.platform.hashing import hmac_sha256_hex, sha256_hex_bytes


_PASSWORD_ENVELOPE_PURPOSE_SCOPES = {
    "login": "login-password",
    "register": "register-password",
    "setup": "setup-password",
    "sudo": "sudo-password",
    "settings-current": "settings-password",
    "settings-new": "settings-password",
    "settings-admin-new": "settings-admin-password",
}
_ALLOWED_PASSWORD_ENVELOPE_SCOPES = set(_PASSWORD_ENVELOPE_PURPOSE_SCOPES.values())
_PASSWORD_ENVELOPE_TTL_SEC = 30
_PASSWORD_ENVELOPE_MAX_ENTRIES = 128
_PASSWORD_ENVELOPE_RATE_WINDOW_SEC = 10.0
_PASSWORD_ENVELOPE_RATE_MAX = 128


@dataclass
class _PasswordEnvelopeEntry:
    private_key: rsa.RSAPrivateKey
    scope: str
    purpose: str
    username: str
    csrf_token: str
    public_key_hash: str
    expires_at: int


def _b64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def _b64url_decode(payload: str) -> bytes:
    text = str(payload or "").strip()
    if not text:
        raise ValueError("missing password envelope ciphertext")
    padded = text + ("=" * ((4 - (len(text) % 4)) % 4))
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ValueError("invalid password envelope ciphertext") from exc


def normalize_password_envelope_scope(scope: str) -> str:
    """Normalize the small fixed set of password envelope scopes."""

    safe_scope = str(scope or "").strip().lower()
    if safe_scope not in _ALLOWED_PASSWORD_ENVELOPE_SCOPES:
        raise ValueError("invalid password envelope scope")
    return safe_scope


def normalize_password_envelope_purpose(purpose: str) -> str:
    """Normalize the small fixed set of password envelope purposes."""

    safe_purpose = str(purpose or "").strip().lower()
    if safe_purpose not in _PASSWORD_ENVELOPE_PURPOSE_SCOPES:
        raise ValueError("invalid password envelope purpose")
    return safe_purpose


def normalize_password_envelope_scope_purpose(scope: str, purpose: str) -> tuple[str, str]:
    safe_scope = normalize_password_envelope_scope(scope)
    safe_purpose = normalize_password_envelope_purpose(purpose)
    if _PASSWORD_ENVELOPE_PURPOSE_SCOPES[safe_purpose] != safe_scope:
        raise ValueError("invalid password envelope scope")
    return safe_scope, safe_purpose


def _envelope_signature(
    *,
    scope: str,
    purpose: str,
    csrf_token: str,
    username: str,
    key_id: str,
    public_key_hash: str,
    expires_at: int,
) -> str:
    payload = (
        "password-envelope-v2|"
        f"{scope}|{purpose}|{csrf_token}|{username}|{key_id}|{public_key_hash}|{int(expires_at)}"
    ).encode("utf-8")
    return hmac_sha256_hex(config.password_form_csrf_secret, payload)


class PasswordEnvelopeStore:
    """In-memory one-time RSA envelope store for password verifier submission."""

    def __init__(self, *, key_factory: Callable[[], rsa.RSAPrivateKey] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _PasswordEnvelopeEntry] = {}
        self._issue_times_by_key: dict[str, list[float]] = {}
        self._key_factory = key_factory or self._generate_private_key

    @staticmethod
    def _generate_private_key() -> rsa.RSAPrivateKey:
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _prune_locked(self, now_ts: int) -> None:
        expired = [
            key_id
            for key_id, entry in self._entries.items()
            if int(entry.expires_at) <= int(now_ts)
        ]
        for key_id in expired:
            self._entries.pop(key_id, None)
        if len(self._entries) <= _PASSWORD_ENVELOPE_MAX_ENTRIES:
            return
        overflow = len(self._entries) - _PASSWORD_ENVELOPE_MAX_ENTRIES
        oldest = sorted(self._entries.items(), key=lambda item: int(item[1].expires_at))
        for key_id, _entry in oldest[:overflow]:
            self._entries.pop(key_id, None)

    def _check_rate_limit_locked(self, rate_key: str, now_mono: float) -> None:
        if not rate_key:
            return
        cutoff = float(now_mono) - _PASSWORD_ENVELOPE_RATE_WINDOW_SEC
        fresh = [ts for ts in self._issue_times_by_key.get(rate_key, []) if float(ts) > cutoff]
        if len(fresh) >= _PASSWORD_ENVELOPE_RATE_MAX:
            self._issue_times_by_key[rate_key] = fresh
            raise ValueError("too many password envelope requests")
        fresh.append(float(now_mono))
        self._issue_times_by_key[rate_key] = fresh
        stale_keys = [
            key
            for key, timestamps in self._issue_times_by_key.items()
            if not any((float(ts) > cutoff for ts in timestamps))
        ]
        for key in stale_keys:
            self._issue_times_by_key.pop(key, None)

    def issue(
        self,
        *,
        scope: str,
        purpose: str,
        username: str,
        csrf_token: str,
        rate_key: str = "",
    ) -> dict[str, object]:
        """Create a short-lived public key bound to a password CSRF token."""

        safe_scope, safe_purpose = normalize_password_envelope_scope_purpose(scope, purpose)
        safe_username = str(username or "").strip()
        safe_csrf = str(csrf_token or "").strip()
        if not verify_password_form_csrf_token(safe_csrf, safe_scope):
            raise ValueError("invalid password token")
        with self._lock:
            self._check_rate_limit_locked(str(rate_key or "").strip(), time.monotonic())
        private_key = self._key_factory()
        public_der = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_key_hash = sha256_hex_bytes(public_der)
        key_id = f"pe-{secrets.token_hex(16)}"
        expires_at = int(time.time()) + _PASSWORD_ENVELOPE_TTL_SEC
        token = _envelope_signature(
            scope=safe_scope,
            purpose=safe_purpose,
            csrf_token=safe_csrf,
            username=safe_username,
            key_id=key_id,
            public_key_hash=public_key_hash,
            expires_at=expires_at,
        )
        entry = _PasswordEnvelopeEntry(
            private_key=private_key,
            scope=safe_scope,
            purpose=safe_purpose,
            username=safe_username,
            csrf_token=safe_csrf,
            public_key_hash=public_key_hash,
            expires_at=expires_at,
        )
        with self._lock:
            self._prune_locked(int(time.time()))
            self._entries[key_id] = entry
            self._prune_locked(int(time.time()))
        return {
            "key_id": key_id,
            "public_key": _b64url_encode(public_der),
            "envelope_token": token,
            "expires_at": expires_at,
        }

    def consume(
        self,
        *,
        scope: str,
        purpose: str,
        username: str,
        csrf_token: str,
        key_id: str,
        envelope_token: str,
        encrypted_verifier: str,
    ) -> str:
        """Consume an envelope and return the decrypted canonical verifier."""

        safe_scope, safe_purpose = normalize_password_envelope_scope_purpose(scope, purpose)
        safe_username = str(username or "").strip()
        safe_csrf = str(csrf_token or "").strip()
        safe_key_id = str(key_id or "").strip()
        safe_token = str(envelope_token or "").strip().lower()
        now_ts = int(time.time())
        with self._lock:
            self._prune_locked(now_ts)
            entry = self._entries.get(safe_key_id)
            if entry is None:
                raise ValueError("invalid password envelope")
            if int(entry.expires_at) <= now_ts:
                self._entries.pop(safe_key_id, None)
                raise ValueError("invalid password envelope")
            expected_token = _envelope_signature(
                scope=safe_scope,
                purpose=safe_purpose,
                csrf_token=safe_csrf,
                username=safe_username,
                key_id=safe_key_id,
                public_key_hash=entry.public_key_hash,
                expires_at=entry.expires_at,
            )
            if (
                entry.scope != safe_scope
                or entry.purpose != safe_purpose
                or entry.username != safe_username
                or entry.csrf_token != safe_csrf
                or not secrets.compare_digest(expected_token, safe_token)
            ):
                raise ValueError("invalid password envelope")
            self._entries.pop(safe_key_id, None)
        try:
            ciphertext = _b64url_decode(encrypted_verifier)
            plaintext = entry.private_key.decrypt(
                ciphertext,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            verifier = plaintext.decode("utf-8")
        except Exception as exc:
            raise ValueError("invalid password envelope") from exc
        return normalize_password_verifier_hex(verifier)


password_envelope_store = PasswordEnvelopeStore()
