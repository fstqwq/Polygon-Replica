from __future__ import annotations

import secrets
import time

from app.impl.runtime.config import config
from app.service.platform.hashing import hmac_sha256_hex

_C = config.constants

def _password_form_csrf_signature(scope: str, issued_at: int, nonce: str) -> str:
    payload = f'{scope}|{issued_at}|{nonce}'.encode('utf-8')
    return hmac_sha256_hex(config.password_form_csrf_secret, payload)

def issue_password_form_csrf_token(scope: str) -> str:
    safe_scope = str(scope or '').strip().lower()
    issued_at = int(time.time())
    nonce = secrets.token_hex(16)
    signature = _password_form_csrf_signature(safe_scope, issued_at, nonce)
    return f'{issued_at}.{nonce}.{signature}'

def verify_password_form_csrf_token(token: str, scope: str) -> bool:
    raw = str(token or '').strip()
    if not raw:
        return False
    parts = raw.split('.')
    if len(parts) != 3:
        return False
    issued_raw, nonce, provided_sig = parts
    if not _C.HEX_32_RE.fullmatch(str(nonce or '').lower()):
        return False
    if not _C.HEX_64_RE.fullmatch(str(provided_sig or '').lower()):
        return False
    try:
        issued_at = int(issued_raw)
    except Exception:
        return False
    now_ts = int(time.time())
    if issued_at <= 0 or issued_at > now_ts + 60:
        return False
    if now_ts - issued_at > _C.PASSWORD_FORM_CSRF_TTL_SEC:
        return False
    expected = _password_form_csrf_signature(str(scope or '').strip().lower(), issued_at, nonce)
    return secrets.compare_digest(expected, str(provided_sig or '').lower())
