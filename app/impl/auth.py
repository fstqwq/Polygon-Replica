from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from app.impl.config import config
from app.db import now_iso

_C = config.constants

def _startup_cleanup_runtime_cache() -> None:
    try:
        config.runtime_cache_service.cleanup_cache(force=True)
    except Exception as exc:
        warnings.warn(f'startup runtime cache cleanup failed: {exc}', RuntimeWarning)


def startup() -> None:
    config.db.init()
    config.invocation_backend_service.refresh_from_env()
    config.worker_queue_service.start()
    _startup_cleanup_runtime_cache()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f'shutdown worker queue stop failed: {exc}', RuntimeWarning)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso_utc(raw: str) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        value = datetime.fromisoformat(text)
    except Exception:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def _format_local_time(raw: object) -> str:
    if isinstance(raw, datetime):
        try:
            return raw.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return raw.strftime('%Y-%m-%d %H:%M:%S')
    text = str(raw or '').strip()
    if not text:
        return '-'
    parsed = _parse_iso_utc(text)
    if parsed is None:
        return text
    try:
        return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return parsed.strftime('%Y-%m-%d %H:%M:%S')
config.templates.env.filters['local_time'] = _format_local_time

def _normalize_flash_message(raw: object) -> str:
    text = str(raw or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return ''
    text = ' '.join((part for part in text.split('\n') if part.strip()))
    if len(text) > _C.FLASH_MESSAGE_MAX_LEN:
        text = text[:_C.FLASH_MESSAGE_MAX_LEN].rstrip()
    return text

def _decode_flash_queue(raw_cookie: str) -> list[str]:
    token = str(raw_cookie or '').strip()
    if not token:
        return []
    try:
        padded = token + '=' * ((4 - len(token) % 4) % 4)
        payload = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
        raw_queue = json.loads(payload)
    except Exception:
        return []
    if not isinstance(raw_queue, list):
        return []
    queue: list[str] = []
    for item in raw_queue:
        normalized = _normalize_flash_message(item)
        if not normalized:
            continue
        queue.append(normalized)
        if len(queue) >= _C.FLASH_QUEUE_MAX_ITEMS:
            break
    return queue

def _encode_flash_queue(queue: list[str]) -> str:
    safe_items: list[str] = []
    for item in queue:
        normalized = _normalize_flash_message(item)
        if not normalized:
            continue
        safe_items.append(normalized)
        if len(safe_items) >= _C.FLASH_QUEUE_MAX_ITEMS:
            break
    if not safe_items:
        return ''
    payload = json.dumps(safe_items, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

def _set_flash_cookie(response, queue: list[str]) -> None:
    encoded = _encode_flash_queue(queue)
    if not encoded:
        response.delete_cookie(_C.FLASH_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
        return
    response.set_cookie(_C.FLASH_COOKIE_NAME, encoded, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.FLASH_COOKIE_MAX_AGE, path='/')

def _extract_message_from_redirect_target(target: str) -> tuple[str, str]:
    url = str(target or '').strip() or '/'
    parsed = urlparse(url)
    if not parsed.query:
        return (url, '')
    kept: list[tuple[str, str]] = []
    message = ''
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == 'message':
            if not message:
                message = _normalize_flash_message(value)
            continue
        kept.append((key, value))
    cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(kept, doseq=True), parsed.fragment))
    if parsed.scheme or parsed.netloc:
        return (cleaned or url, message)
    if not cleaned:
        return (url, message)
    return (cleaned, message)

def _apply_security_headers(response) -> None:
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")

def _redirect_response(url: str, status_code: int=303, message: str='') -> RedirectResponse:
    target, _ = _extract_message_from_redirect_target(url)
    response = RedirectResponse(target, status_code=status_code)
    safe_message = _normalize_flash_message(message)
    if safe_message:
        _set_flash_cookie(response, [safe_message])
    _apply_security_headers(response)
    return response

def _template_response(request: Request, template_name: str, context: dict | None=None):
    payload = dict(context or {})
    backend_render_ms: int | None = None
    started = getattr(request.state, 'request_started_at', None)
    if isinstance(started, (int, float)):
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms >= 0:
            backend_render_ms = int(round(elapsed_ms))
    if 'backend_render_ms' not in payload:
        payload['backend_render_ms'] = backend_render_ms
    raw_cookie = str(request.cookies.get(_C.FLASH_COOKIE_NAME, '') or '').strip()
    queue = _decode_flash_queue(raw_cookie)
    fallback_message = _normalize_flash_message(payload.get('message', ''))
    message = queue[0] if queue else fallback_message
    payload['message'] = message
    response = config.templates.TemplateResponse(request, template_name, payload)
    if queue:
        _set_flash_cookie(response, queue[1:])
    elif raw_cookie:
        _set_flash_cookie(response, [])
    _apply_security_headers(response)
    return response

def _password_hash(password: str, salt_hex: str, iterations: int) -> str:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, int(iterations))
    return digest.hex()

def _normalize_password_salt_hex(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not _C.HEX_32_RE.fullmatch(raw):
        raise ValueError('invalid password salt')
    return raw

def _normalize_password_verifier_hex(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not _C.HEX_64_RE.fullmatch(raw):
        raise ValueError('invalid password verifier')
    return raw

def _normalize_password_iters(value: object) -> int:
    try:
        iters = int(value)
    except Exception as exc:
        raise ValueError('invalid password iterations') from exc
    if iters < 10000 or iters > 10000000:
        raise ValueError('invalid password iterations')
    return iters

def _password_form_csrf_signature(scope: str, issued_at: int, nonce: str) -> str:
    payload = f'{scope}|{issued_at}|{nonce}'.encode('utf-8')
    return hmac.new(config.password_form_csrf_secret, payload, hashlib.sha256).hexdigest()

def _issue_password_form_csrf_token(scope: str) -> str:
    safe_scope = str(scope or '').strip().lower()
    issued_at = int(time.time())
    nonce = secrets.token_hex(16)
    signature = _password_form_csrf_signature(safe_scope, issued_at, nonce)
    return f'{issued_at}.{nonce}.{signature}'

def _verify_password_form_csrf_token(token: str, scope: str) -> bool:
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

def _password_proof_from_verifier(csrf_token: str, verifier_hex: str) -> str:
    safe_csrf = str(csrf_token or '').strip()
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    digest = hashlib.sha256(f'{safe_csrf}{safe_verifier}'.encode('utf-8')).hexdigest()
    return digest

def _dummy_password_salt_hex(username: str) -> str:
    safe_user = str(username or '').strip().lower()
    digest = hmac.new(config.password_form_csrf_secret, f'dummy-meta|{safe_user}'.encode('utf-8'), hashlib.sha256).hexdigest()
    return digest[:32]

def _password_meta_for_username(username: str) -> tuple[str, int]:
    row = _lookup_user_auth(username)
    if row is None:
        return (_dummy_password_salt_hex(username), int(_C.PASSWORD_HASH_ITERS))
    verifier = str(row['password_hash'] or '').strip().lower()
    salt_hex = str(row['password_salt'] or '').strip().lower()
    try:
        iterations = int(row['password_iters'] or 0)
    except Exception:
        iterations = 0
    if _C.HEX_64_RE.fullmatch(verifier) and _C.HEX_32_RE.fullmatch(salt_hex) and (iterations > 0):
        return (salt_hex, iterations)
    return (_dummy_password_salt_hex(username), int(_C.PASSWORD_HASH_ITERS))

def _validate_password(password: str) -> str:
    raw = str(password or '')
    if len(raw) < _C.PASSWORD_MIN_LEN:
        raise ValueError(f'password must be at least {_C.PASSWORD_MIN_LEN} characters')
    if len(raw) > _C.PASSWORD_MAX_LEN:
        raise ValueError(f'password must be at most {_C.PASSWORD_MAX_LEN} characters')
    return raw

def _lookup_user_auth(username: str):
    safe = str(username or '').strip()
    if not _C.USER_IDENT_RE.fullmatch(safe):
        return None
    return config.db.fetch_one('SELECT id,username,password_hash,password_salt,password_iters FROM users WHERE username=?', [safe])

def _registered_user_count() -> int:
    row = config.db.fetch_one("SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''", [])
    if row is None:
        return 0
    try:
        return max(0, int(row['c'] or 0))
    except Exception:
        return 0

def _has_registered_users() -> bool:
    return _registered_user_count() > 0

def _normalize_username_required(value: str) -> str:
    safe = str(value or '').strip()
    if len(safe) > 64 or not _C.USER_IDENT_RE.fullmatch(safe):
        raise ValueError(_C.USERNAME_RULE_MESSAGE)
    return safe

def _set_user_password_verifier(user_id: int, verifier_hex: str, salt_hex: str, iterations: int) -> None:
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    config.db.execute('UPDATE users SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=? WHERE id=?', [safe_verifier, safe_salt, safe_iters, now_iso(), int(user_id)])

def _set_user_password(user_id: int, password: str) -> None:
    safe_password = _validate_password(password)
    salt_hex = secrets.token_hex(16)
    digest = _password_hash(safe_password, salt_hex, _C.PASSWORD_HASH_ITERS)
    _set_user_password_verifier(int(user_id), digest, salt_hex, int(_C.PASSWORD_HASH_ITERS))

def _create_user_with_password_verifier(username: str, verifier_hex: str, salt_hex: str, iterations: int) -> int:
    safe_user = _normalize_username_required(username)
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    now = now_iso()
    with config.db.conn() as conn:
        has_registered_user = conn.execute("SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1").fetchone() is not None
        admin_candidates = [0] if has_registered_user else [1, 0]
        inserted = False
        for is_admin in admin_candidates:
            try:
                conn.execute('\n                    INSERT INTO users(\n                        username,password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin\n                    )\n                    VALUES(?,?,?,?,?,?,?)\n                    ', [safe_user, safe_verifier, safe_salt, safe_iters, now, now, int(is_admin)])
                inserted = True
                break
            except sqlite3.IntegrityError as exc:
                msg = str(exc or '').strip().lower()
                if 'users.username' in msg:
                    raise ValueError('user already exists') from exc
                if int(is_admin) == 1:
                    continue
                raise
        if not inserted:
            raise RuntimeError('failed to create user')
        row = conn.execute('SELECT id FROM users WHERE username=?', [safe_user]).fetchone()
        if row is None:
            raise RuntimeError('failed to create user')
        conn.commit()
        return int(row['id'])

def _create_user_with_password(username: str, password: str) -> int:
    safe_password = _validate_password(password)
    salt_hex = secrets.token_hex(16)
    digest = _password_hash(safe_password, salt_hex, _C.PASSWORD_HASH_ITERS)
    return _create_user_with_password_verifier(username, digest, salt_hex, int(_C.PASSWORD_HASH_ITERS))

def _bootstrap_super_admin_with_password_verifier(username: str, verifier_hex: str, salt_hex: str, iterations: int) -> int:
    safe_user = _normalize_username_required(username)
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    now = now_iso()
    with config.db.conn() as conn:
        has_registered_user = conn.execute("SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1").fetchone() is not None
        if has_registered_user:
            raise ValueError('setup already completed')
        existing = conn.execute("SELECT id,password_hash FROM users WHERE username=?", [safe_user]).fetchone()
        if existing is None:
            try:
                conn.execute(
                    """
                    INSERT INTO users(
                        username,password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin
                    )
                    VALUES(?,?,?,?,?,?,1)
                    """,
                    [safe_user, safe_verifier, safe_salt, safe_iters, now, now],
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc or '').strip().lower()
                if 'users.username' in msg:
                    raise ValueError('setup failed; username is unavailable') from exc
                raise
            existing = conn.execute("SELECT id,password_hash FROM users WHERE username=?", [safe_user]).fetchone()
            if existing is None:
                raise RuntimeError('failed to create super admin')
        else:
            current_hash = str(existing['password_hash'] or '').strip()
            if current_hash:
                raise ValueError('setup failed; username is unavailable')
            conn.execute(
                """
                UPDATE users
                SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=?,is_system_admin=1
                WHERE id=?
                """,
                [safe_verifier, safe_salt, safe_iters, now, int(existing['id'])],
            )
        user_id = int(existing['id'])
        conn.execute("UPDATE users SET is_system_admin=0 WHERE id<>?", [user_id])
        conn.commit()
        return user_id

def _bootstrap_super_admin_with_password(username: str, password: str) -> int:
    safe_password = _validate_password(password)
    salt_hex = secrets.token_hex(16)
    digest = _password_hash(safe_password, salt_hex, _C.PASSWORD_HASH_ITERS)
    return _bootstrap_super_admin_with_password_verifier(username, digest, salt_hex, int(_C.PASSWORD_HASH_ITERS))

def _verify_user_password(user_row, password: str) -> bool:
    if user_row is None:
        return False
    expected = str(user_row['password_hash'] or '')
    salt_hex = str(user_row['password_salt'] or '')
    iterations = int(user_row['password_iters'] or 0)
    if not expected or not salt_hex or iterations <= 0:
        return False
    try:
        actual = _password_hash(str(password or ''), salt_hex, iterations)
    except Exception:
        return False
    return secrets.compare_digest(expected, actual)

def _create_session_for_user(user_id: int) -> str:
    uid = int(user_id)
    expires = (_utc_now() + timedelta(seconds=_C.AUTH_COOKIE_MAX_AGE)).isoformat()
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
        sid = f's-{secrets.token_hex(12)}'
        try:
            config.db.execute('INSERT INTO auth_sessions(id,user_id,token_hash,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)', [sid, uid, token_hash, now_iso(), expires])
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError('failed to create auth session')

def _revoke_session_token(token: str) -> None:
    raw = str(token or '').strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return
    token_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    config.db.execute('UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', [now_iso(), token_hash])

def _session_identity(request: Request) -> dict | None:
    raw = str(request.cookies.get(_C.AUTH_COOKIE_NAME, '')).strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return None
    token_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    row = config.db.fetch_one('\n        SELECT s.id AS session_id,s.user_id,s.expires_at,u.username\n        FROM auth_sessions s\n        JOIN users u ON u.id=s.user_id\n        WHERE s.token_hash=? AND s.revoked_at IS NULL\n        ', [token_hash])
    if row is None:
        return None
    expires_at = _parse_iso_utc(str(row['expires_at'] or ''))
    if expires_at is None or expires_at <= _utc_now():
        _revoke_session_token(raw)
        return None
    return {'session_id': row['session_id'], 'user_id': int(row['user_id']), 'username': str(row['username']), 'token': raw}

def _session_user(request: Request) -> str:
    identity = _session_identity(request)
    if identity is None:
        return ''
    return str(identity['username'])

def _safe_next_path(raw: str | None, fallback: str='/') -> str:
    candidate = str(raw or '').strip()
    if not candidate:
        return fallback
    if not candidate.startswith('/') or candidate.startswith('//'):
        return fallback
    return candidate

def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += f'?{request.url.query}'
    if not _has_registered_users():
        return _redirect_response(f'/setup?next={quote_plus(target)}', status_code=303)
    return _redirect_response(f'/login?next={quote_plus(target)}', status_code=303)

def _request_origin_value(raw: str) -> str:
    value = str(raw or '').strip()
    if not value:
        return ''
    parsed = urlparse(value)
    scheme = str(parsed.scheme or '').strip().lower()
    netloc = str(parsed.netloc or '').strip().lower()
    if not scheme or not netloc:
        return ''
    return f'{scheme}://{netloc}'

def _expected_request_origin(request: Request) -> str:
    return f'{str(request.url.scheme).strip().lower()}://{str(request.url.netloc).strip().lower()}'

def _enforce_same_origin_state_change(request: Request | None) -> None:
    if request is None:
        return
    method = str(request.method or '').strip().upper()
    if method in {'GET', 'HEAD', 'OPTIONS', 'TRACE'}:
        return
    expected = _expected_request_origin(request)
    origin = _request_origin_value(str(request.headers.get('origin') or ''))
    if origin:
        if origin != expected:
            raise HTTPException(status_code=403, detail='cross-site request blocked')
        return
    referer = _request_origin_value(str(request.headers.get('referer') or ''))
    if referer:
        if referer != expected:
            raise HTTPException(status_code=403, detail='cross-site request blocked')
        return
    raise HTTPException(status_code=403, detail='missing origin/referrer for state-changing request')

def _login_rate_limit_key(username: str, request: Request | None) -> str:
    safe_user = str(username or '').strip().lower()
    ip = ''
    if request is not None:
        forwarded = str(request.headers.get('x-forwarded-for') or '').strip()
        if forwarded:
            ip = str(forwarded.split(',', 1)[0]).strip()
        if not ip:
            client = request.client
            ip = str(client.host).strip() if client is not None and client.host else ''
    if not ip:
        ip = 'unknown'
    return f'{ip}|{safe_user}'

def _login_rate_limit_check(key: str) -> None:
    now_monotonic = time.monotonic()
    with config.login_rate_limit_lock:
        state = config.login_rate_limit_state.get(key)
        if state is None:
            return
        blocked_until = float(state.get('blocked_until') or 0.0)
        if blocked_until > now_monotonic:
            wait_sec = max(1, int(round(blocked_until - now_monotonic)))
            raise ValueError(f'too many failed attempts; retry in {wait_sec}s')
        window_start = float(state.get('window_start') or 0.0)
        if window_start <= 0.0 or now_monotonic - window_start > _C.LOGIN_RATE_LIMIT_WINDOW_SEC:
            config.login_rate_limit_state.pop(key, None)

def _login_rate_limit_fail(key: str) -> None:
    now_monotonic = time.monotonic()
    with config.login_rate_limit_lock:
        state = config.login_rate_limit_state.get(key)
        if state is None:
            state = {'window_start': now_monotonic, 'failures': 0, 'blocked_until': 0.0}
        window_start = float(state.get('window_start') or 0.0)
        if window_start <= 0.0 or now_monotonic - window_start > _C.LOGIN_RATE_LIMIT_WINDOW_SEC:
            state = {'window_start': now_monotonic, 'failures': 0, 'blocked_until': 0.0}
        failures = int(state.get('failures') or 0) + 1
        state['failures'] = failures
        if failures >= _C.LOGIN_RATE_LIMIT_MAX_FAILURES:
            state['blocked_until'] = now_monotonic + _C.LOGIN_RATE_LIMIT_BLOCK_SEC
            state['window_start'] = now_monotonic
            state['failures'] = 0
        config.login_rate_limit_state[key] = state

def _login_rate_limit_success(key: str) -> None:
    with config.login_rate_limit_lock:
        config.login_rate_limit_state.pop(key, None)

async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith('/static/') or path in {'/login', '/register', '/setup'}:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    protected = path == '/' or path.startswith('/problems/') or path.startswith('/switch-') or (path == '/logout')
    if not protected:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    user = _session_user(request)
    if not user:
        response = _login_redirect(request)
        _apply_security_headers(response)
        return response
    _enforce_same_origin_state_change(request)
    tm = _C.TOPLEVEL_USER_PATH_RE.match(path)
    if tm:
        if tm.group('user') != user:
            section = tm.group('section')
            rest = tm.group('rest') or ''
            target = f'/problems/{user}/{section}{rest}'
            if request.url.query:
                target += f'?{request.url.query}'
            return _redirect_response(target, status_code=303)
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    pm = _C.PROBLEM_USER_PATH_RE.match(path)
    if pm and pm.group('user') != user:
        rest = pm.group('rest') or ''
        target = f'/problems/{pm.group('problem')}/{user}{rest}'
        if request.url.query:
            target += f'?{request.url.query}'
        return _redirect_response(target, status_code=303)
    response = await call_next(request)
    _apply_security_headers(response)
    return response
