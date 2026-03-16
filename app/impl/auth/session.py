from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import Request

from app.db import now_iso
from app.impl.runtime.config import config
from app.impl.auth.shared import parse_iso_utc
from app.service.platform.hashing import sha256_hex_text

_C = config.constants

def create_session_for_user(user_id: int) -> str:
    uid = int(user_id)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=_C.AUTH_COOKIE_MAX_AGE)).isoformat()
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = sha256_hex_text(token)
        sid = f's-{secrets.token_hex(12)}'
        try:
            config.db.execute('INSERT INTO auth_sessions(id,user_id,token_hash,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)', [sid, uid, token_hash, now_iso(), expires])
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError('failed to create auth session')

def create_sudo_session_for_user(user_id: int, scope: str) -> str:
    uid = int(user_id)
    safe_scope = str(scope or '').strip().lower()
    if not safe_scope:
        raise ValueError('invalid sudo scope')
    expires = (datetime.now(timezone.utc) + timedelta(seconds=int(_C.SUDO_COOKIE_MAX_AGE))).isoformat()
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = sha256_hex_text(token)
        sid = f'sudo-{secrets.token_hex(12)}'
        try:
            config.db.execute(
                'INSERT INTO sudo_sessions(id,user_id,scope,token_hash,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)',
                [sid, uid, safe_scope, token_hash, now_iso(), expires],
            )
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError('failed to create sudo session')

def revoke_session_token(token: str) -> None:
    raw = str(token or '').strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return
    token_hash = sha256_hex_text(raw)
    config.db.execute('UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', [now_iso(), token_hash])

def revoke_sudo_session_token(token: str) -> None:
    raw = str(token or '').strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return
    token_hash = sha256_hex_text(raw)
    config.db.execute('UPDATE sudo_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', [now_iso(), token_hash])

def revoke_sudo_sessions_for_user(user_id: int) -> None:
    config.db.execute('UPDATE sudo_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', [now_iso(), int(user_id)])

def session_identity(request: Request) -> dict | None:
    raw = str(request.cookies.get(_C.AUTH_COOKIE_NAME, '')).strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return None
    token_hash = sha256_hex_text(raw)
    row = config.db.fetch_one('\n        SELECT s.id AS session_id,s.user_id,s.expires_at,u.username\n        FROM auth_sessions s\n        JOIN users u ON u.id=s.user_id\n        WHERE s.token_hash=? AND s.revoked_at IS NULL\n        ', [token_hash])
    if row is None:
        return None
    expires_at = parse_iso_utc(str(row['expires_at'] or ''))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        revoke_session_token(raw)
        return None
    return {'session_id': row['session_id'], 'user_id': int(row['user_id']), 'username': str(row['username']), 'token': raw}

def _sudo_identity(request: Request, scope: str) -> dict | None:
    safe_scope = str(scope or '').strip().lower()
    if not safe_scope:
        return None
    raw = str(request.cookies.get(_C.SUDO_COOKIE_NAME, '')).strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return None
    token_hash = sha256_hex_text(raw)
    row = config.db.fetch_one(
        '\n        SELECT s.id AS sudo_session_id,s.user_id,s.scope,s.expires_at\n        FROM sudo_sessions s\n        WHERE s.token_hash=? AND s.revoked_at IS NULL\n        ',
        [token_hash],
    )
    if row is None:
        return None
    row_scope = str(row['scope'] or '').strip().lower()
    if row_scope != safe_scope:
        revoke_sudo_session_token(raw)
        return None
    expires_at = parse_iso_utc(str(row['expires_at'] or ''))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        revoke_sudo_session_token(raw)
        return None
    return {'sudo_session_id': str(row['sudo_session_id']), 'user_id': int(row['user_id']), 'scope': row_scope, 'token': raw}

def has_sudo_session(request: Request, *, user_id: int, scope: str) -> bool:
    identity = _sudo_identity(request, scope)
    if identity is None:
        return False
    return int(identity['user_id']) == int(user_id)

def session_user(request: Request) -> str:
    identity = session_identity(request)
    if identity is None:
        return ''
    return str(identity['username'])

