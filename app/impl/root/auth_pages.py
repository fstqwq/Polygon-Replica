import app.main_constant as _K

import ipaddress
import re
import secrets

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from urllib.parse import quote_plus

from app.service.platform.hashing import sha256_hex_text
from app.impl.auth.shared import (
    bootstrap_super_admin_with_password_verifier,
    create_user_with_password_verifier,
    enforce_same_origin_state_change,
    has_registered_users,
    login_rate_limit_check,
    login_rate_limit_fail,
    login_rate_limit_key,
    login_rate_limit_success,
    lookup_user_auth,
    normalize_password_iters,
    normalize_password_salt_hex,
    normalize_username_required,
    password_meta_for_username,
    redirect_response,
    safe_next_path,
    template_response,
)
from app.impl.auth.session import (
    create_session_for_user,
    create_sudo_session_for_user,
    has_sudo_session,
    revoke_session_token,
    revoke_sudo_session_token,
    session_identity,
    session_user,
)
from app.impl.auth.csrf import (
    issue_password_form_csrf_token,
    verify_password_form_csrf_token,
)
from app.impl.auth.password_envelope import (
    normalize_password_envelope_scope_purpose,
    password_envelope_store,
)
from app.impl.runtime.dependency import runtime
from app.main_util import form_text
from app.service.auth.password_hash import password_verifier_storage_hash

_REGISTRATION_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REGISTRATION_CODE_LENGTH = 12


def _setup_config_rows() -> list[dict[str, str]]:
    return [
        {'name': 'POLYGON_REPLICA_DB', 'value': str(runtime().settings.db_path)},
        {'name': 'POLYGON_REPLICA_BARE_ROOT', 'value': str(runtime().settings.bare_root)},
        {'name': 'POLYGON_REPLICA_WORKSPACE_ROOT', 'value': str(runtime().settings.workspace_root)},
        {'name': 'POLYGON_REPLICA_ARTIFACTS_ROOT', 'value': str(runtime().settings.artifacts_root)},
        {'name': 'POLYGON_REPLICA_CACHE_ROOT', 'value': str(runtime().settings.cache_root)},
        {'name': 'POLYGON_REPLICA_CONTEST_SOURCE_ROOT', 'value': str(runtime().settings.contest_source_root)},
        {'name': 'POLYGON_REPLICA_BACKUP_ROOT', 'value': str(runtime().settings.backup_root)},
    ]



def _client_ip_for_auth_rate_limit(request: Request | None) -> str:
    if request is None:
        return "unknown"
    client = request.client
    client_host = str(client.host).strip() if client is not None and client.host else ""
    trusted_proxy = False
    try:
        trusted_proxy = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or client_host or "unknown"
    return client_host or "unknown"


def _request_user_agent(request: Request | None) -> str:
    if request is None:
        return ""
    return str(request.headers.get("user-agent") or "").strip()[:512]


def _is_system_admin_auth_row(row: dict[str, object] | None) -> bool:
    if row is None:
        return False
    try:
        user_id = int(row["id"])
    except Exception:
        return False
    return runtime().access_query.is_system_admin(user_id)


def _normalize_registration_email(value: str) -> tuple[str, str]:
    email = form_text(value).strip()
    if any(ch in email for ch in ("\x00", "\r", "\n")):
        raise ValueError("registration failed; invalid email")
    normalized = email.lower()
    pattern = str(runtime().config_values.AUTH_EMAIL_ALLOW_REGEX or "").strip()
    try:
        allowed = re.compile(pattern)
    except re.error as exc:
        raise ValueError("registration failed; email allow regex is invalid") from exc
    if not allowed.fullmatch(normalized):
        raise ValueError("registration failed; email is not allowed")
    return email, normalized


def _hit_auth_rate_limit(bucket_key: str, *, limit: int, window_sec: int) -> dict[str, object]:
    return runtime().auth_service.hit_rate_limit(
        bucket_key,
        limit=int(limit),
        window_sec=int(window_sec),
    )


def _enforce_auth_rate_limit(
    *,
    bucket_key: str,
    limit: int,
    window_sec: int,
    message: str = "too many registration attempts",
) -> None:
    hit = _hit_auth_rate_limit(bucket_key, limit=limit, window_sec=window_sec)
    if bool(hit["allowed"]):
        return
    raise ValueError(f"{message}; retry in {int(hit['retry_after_sec'])}s")


def _new_registration_verification_code() -> str:
    raw = "".join(secrets.choice(_REGISTRATION_CODE_ALPHABET) for _ in range(_REGISTRATION_CODE_LENGTH))
    return "-".join(raw[index : index + 4] for index in range(0, len(raw), 4))


def _normalize_registration_verification_code(value: str) -> str:
    normalized = "".join(
        ch for ch in form_text(value).upper() if not ch.isspace() and ch != "-"
    )
    if len(normalized) != _REGISTRATION_CODE_LENGTH:
        raise ValueError("registration verification failed")
    if any(ch not in _REGISTRATION_CODE_ALPHABET for ch in normalized):
        raise ValueError("registration verification failed")
    return normalized


def _registration_expiry_minutes() -> int:
    return max(1, int(round(int(runtime().config_values.AUTH_REGISTER_PENDING_TTL_SEC) / 60.0)))


def setup_page(request: Request):
    user = session_user(request)
    next_path = safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register', '/setup'} else '/problems'
        return redirect_response(target, status_code=303)
    if has_registered_users():
        return redirect_response('/login', status_code=303, message='setup already completed')
    return template_response(request, 'setup.html', {'next_path': next_path, 'password_csrf_token': issue_password_form_csrf_token('setup-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(runtime().config_values.PASSWORD_HASH_ITERS), 'config_rows': _setup_config_rows()})

def setup_submit(
    request: Request,
    username: str=Form(...),
    password: str=Form(''),
    password_confirm: str=Form(''),
    csrf_token: str=Form(''),
    key_id: str=Form(''),
    envelope_token: str=Form(''),
    encrypted_verifier: str=Form(''),
    password_salt: str=Form(''),
    password_iters: str=Form(''),
    confirm_config: str=Form('0'),
    next: str=Form('/'),
):
    enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    try:
        if has_registered_users():
            raise ValueError('setup already completed')
        if str(confirm_config or '').strip() not in {'1', 'true', 'on', 'yes'}:
            raise ValueError('please confirm current system configuration paths')
        safe_user = normalize_username_required(form_text(username))
        password_csrf = form_text(csrf_token).strip()
        salt_value = form_text(password_salt)
        iter_value = form_text(password_iters)
        next_path = form_text(next)
        if not verify_password_form_csrf_token(password_csrf, 'setup-password'):
            raise ValueError('setup failed; invalid csrf token')
        try:
            verifier = password_envelope_store.consume(
                scope='setup-password',
                purpose='setup',
                username=safe_user,
                csrf_token=password_csrf,
                key_id=form_text(key_id),
                envelope_token=form_text(envelope_token),
                encrypted_verifier=form_text(encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError('setup failed; invalid password envelope') from exc
        salt_hex = normalize_password_salt_hex(salt_value)
        iters = normalize_password_iters(iter_value)
        if iters != int(runtime().config_values.PASSWORD_HASH_ITERS):
            raise ValueError('setup failed; invalid password iterations')
        user_id = bootstrap_super_admin_with_password_verifier(safe_user, verifier, salt_hex, iters)
        token = create_session_for_user(int(user_id))
    except ValueError as exc:
        return redirect_response('/setup', status_code=303, message=str(exc))
    target = safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register', '/setup'}:
        target = '/problems'
    response = redirect_response(target, status_code=303)
    response.set_cookie(runtime().config_values.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=runtime().config_values.AUTH_COOKIE_SECURE, max_age=runtime().config_values.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def login_page(request: Request):
    user = session_user(request)
    next_path = safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else '/problems'
        return redirect_response(target, status_code=303)
    return template_response(request, 'login.html', {'next_path': next_path, 'password_csrf_token': issue_password_form_csrf_token('login-password')})

def auth_password_meta(username: str='', csrf_token: str=''):
    if not verify_password_form_csrf_token(csrf_token, 'login-password'):
        raise HTTPException(status_code=400, detail='invalid csrf token')
    salt_hex, iterations = password_meta_for_username(username)
    return {'salt': salt_hex, 'iters': iterations}


def auth_password_envelope(
    request: Request,
    scope: str = 'login-password',
    purpose: str = 'login',
    username: str = '',
    csrf_token: str = '',
):
    """Issue an in-memory one-time public key for password verifier encryption."""

    try:
        safe_scope, safe_purpose = normalize_password_envelope_scope_purpose(scope, purpose)
        safe_username = form_text(username).strip()
        if safe_scope == 'sudo-password':
            identity = session_identity(request)
            if identity is None:
                raise HTTPException(status_code=401, detail='login required')
            safe_username = str(identity['username'])
        elif safe_scope == 'settings-password':
            current_user = session_user(request)
            if not current_user:
                raise HTTPException(status_code=401, detail='login required')
            safe_username = current_user
        elif safe_scope == 'admin-password':
            current_user = session_user(request)
            if not current_user:
                raise HTTPException(status_code=401, detail='login required')
            current_row = lookup_user_auth(current_user)
            if not _is_system_admin_auth_row(current_row):
                raise HTTPException(status_code=403, detail='system admin required')
        payload = password_envelope_store.issue(
            scope=safe_scope,
            purpose=safe_purpose,
            username=safe_username,
            csrf_token=form_text(csrf_token).strip(),
            rate_key=_client_ip_for_auth_rate_limit(request),
        )
        return JSONResponse(payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def login_submit(
    request: Request,
    username: str=Form(...),
    password: str=Form(''),
    csrf_token: str=Form(''),
    key_id: str=Form(''),
    envelope_token: str=Form(''),
    encrypted_verifier: str=Form(''),
    next: str=Form('/'),
):
    enforce_same_origin_state_change(request)
    _ = password
    raw_user = form_text(username).strip()
    password_csrf = form_text(csrf_token).strip()
    next_path = form_text(next)
    rate_limit_key = login_rate_limit_key(raw_user, request)
    try:
        login_rate_limit_check(rate_limit_key)
        safe_user = (
            raw_user
            if _K.USERNAME_MIN_LEN <= len(raw_user) <= _K.USERNAME_MAX_LEN and _K.USER_IDENT_RE.fullmatch(raw_user)
            else ''
        )
        if not safe_user:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        row = lookup_user_auth(safe_user)
        if row is None:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        if int(row.get('is_banned') or 0) == 1:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('account is banned')
        if not verify_password_form_csrf_token(password_csrf, 'login-password'):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        stored_hash = str(row['password_hash'] or '').strip().lower()
        if not _K.HEX_64_RE.fullmatch(stored_hash):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        try:
            verifier = password_envelope_store.consume(
                scope='login-password',
                purpose='login',
                username=raw_user,
                csrf_token=password_csrf,
                key_id=form_text(key_id),
                envelope_token=form_text(envelope_token),
                encrypted_verifier=form_text(encrypted_verifier),
            )
        except ValueError as exc:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password') from exc
        expected_hash = password_verifier_storage_hash(verifier)
        if not secrets.compare_digest(expected_hash, stored_hash):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        login_rate_limit_success(rate_limit_key)
        token = create_session_for_user(int(row['id']))
    except ValueError as exc:
        return redirect_response('/login', status_code=303, message=str(exc))
    target = safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register'}:
        target = '/problems'
    response = redirect_response(target, status_code=303)
    response.set_cookie(runtime().config_values.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=runtime().config_values.AUTH_COOKIE_SECURE, max_age=runtime().config_values.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def register_page(request: Request):
    user = session_user(request)
    next_path = safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else '/problems'
        return redirect_response(target, status_code=303)
    return template_response(request, 'register.html', {'next_path': next_path, 'password_csrf_token': issue_password_form_csrf_token('register-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(runtime().config_values.PASSWORD_HASH_ITERS)})

def register_submit(
    request: Request,
    username: str=Form(...),
    email: str=Form(''),
    password: str=Form(''),
    password_confirm: str=Form(''),
    csrf_token: str=Form(''),
    key_id: str=Form(''),
    envelope_token: str=Form(''),
    encrypted_verifier: str=Form(''),
    password_salt: str=Form(''),
    password_iters: str=Form(''),
    next: str=Form('/'),
    terms_accepted: str=Form(''),
):
    enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    request_ip = _client_ip_for_auth_rate_limit(request)
    user_agent = _request_user_agent(request)
    try:
        _enforce_auth_rate_limit(
            bucket_key="register-submit:global",
            limit=int(runtime().config_values.AUTH_REGISTER_SUBMIT_MAX),
            window_sec=int(runtime().config_values.AUTH_REGISTER_SUBMIT_WINDOW_SEC),
        )
        safe_user = normalize_username_required(form_text(username))
        safe_email, email_normalized = _normalize_registration_email(form_text(email))
        password_csrf = form_text(csrf_token).strip()
        salt_value = form_text(password_salt)
        iter_value = form_text(password_iters)
        next_path = form_text(next)
        terms_value = form_text(terms_accepted)
        conflict = runtime().auth_service.registration_conflict(safe_user, email_normalized)
        if conflict == "username":
            raise ValueError('registration failed; username is unavailable')
        if conflict == "email":
            raise ValueError('registration failed; email is unavailable')
        if terms_value != 'yes':
            raise ValueError('registration failed; terms of use must be accepted')
        if not verify_password_form_csrf_token(password_csrf, 'register-password'):
            raise ValueError('registration failed; invalid csrf token')
        try:
            verifier = password_envelope_store.consume(
                scope='register-password',
                purpose='register',
                username=safe_user,
                csrf_token=password_csrf,
                key_id=form_text(key_id),
                envelope_token=form_text(envelope_token),
                encrypted_verifier=form_text(encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError('registration failed; invalid password envelope') from exc
        salt_hex = normalize_password_salt_hex(salt_value)
        iters = normalize_password_iters(iter_value)
        if iters != int(runtime().config_values.PASSWORD_HASH_ITERS):
            raise ValueError('registration failed; invalid password iterations')
        if not runtime().smtp_config_service.delivery_configured():
            user_id = create_user_with_password_verifier(
                safe_user,
                verifier,
                salt_hex,
                iters,
                email=safe_email,
                email_normalized=email_normalized,
            )
            token = create_session_for_user(int(user_id))
            target = safe_next_path(next_path, '/problems')
            if target in {'/', '/login', '/register'}:
                target = '/problems'
            response = redirect_response(target, status_code=303)
            response.set_cookie(runtime().config_values.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=runtime().config_values.AUTH_COOKIE_SECURE, max_age=runtime().config_values.AUTH_COOKIE_MAX_AGE, path='/')
            return response
        _enforce_auth_rate_limit(
            bucket_key="register-email-send:global",
            limit=int(runtime().config_values.AUTH_REGISTER_EMAIL_GLOBAL_MAX),
            window_sec=int(runtime().config_values.AUTH_REGISTER_EMAIL_GLOBAL_WINDOW_SEC),
            message="too many registration emails",
        )
        _enforce_auth_rate_limit(
            bucket_key=f"register-email-send:email:{email_normalized}",
            limit=int(runtime().config_values.AUTH_REGISTER_EMAIL_SEND_MAX),
            window_sec=int(runtime().config_values.AUTH_REGISTER_EMAIL_SEND_WINDOW_SEC),
            message="too many registration emails",
        )
        verification_code = _new_registration_verification_code()
        token_hash = sha256_hex_text(_normalize_registration_verification_code(verification_code))
        runtime().auth_service.create_pending_registration(
            username=safe_user,
            email=safe_email,
            email_normalized=email_normalized,
            verifier_hex=verifier,
            salt_hex=salt_hex,
            iterations=iters,
            token_hash=token_hash,
            request_ip=request_ip,
            user_agent=user_agent,
            ttl_sec=int(runtime().config_values.AUTH_REGISTER_PENDING_TTL_SEC),
        )
        try:
            runtime().smtp_config_service.send_registration_email(
                recipient=safe_email,
                verification_code=verification_code,
                expires_in_sec=int(runtime().config_values.AUTH_REGISTER_PENDING_TTL_SEC),
            )
        except ValueError as exc:
            raise ValueError("registration failed; verification email could not be sent") from exc
    except ValueError as exc:
        return redirect_response('/register', status_code=303, message=str(exc))
    return redirect_response(
        '/register/verify',
        status_code=303,
        message=f'registration email sent; enter the verification code within {_registration_expiry_minutes()} minutes',
    )


def register_verify_page(request: Request):
    return template_response(
        request,
        'register_verify.html',
        {'expiry_minutes': _registration_expiry_minutes()},
    )


def register_verify(request: Request, code: str = Form("")):
    enforce_same_origin_state_change(request)
    try:
        normalized_code = _normalize_registration_verification_code(code)
        token_hash = sha256_hex_text(normalized_code)
        user_id = runtime().auth_service.activate_pending_registration(token_hash)
        auth_token = create_session_for_user(int(user_id))
    except ValueError as exc:
        try:
            _enforce_auth_rate_limit(
                bucket_key="register-verify-fail:global",
                limit=int(runtime().config_values.AUTH_REGISTER_VERIFY_FAIL_MAX),
                window_sec=int(runtime().config_values.AUTH_REGISTER_VERIFY_FAIL_WINDOW_SEC),
                message="too many registration verification attempts",
            )
        except ValueError as rate_exc:
            return redirect_response('/register/verify', status_code=303, message=str(rate_exc))
        return redirect_response('/register/verify', status_code=303, message=str(exc))
    response = redirect_response('/problems', status_code=303, message='registration verified')
    response.set_cookie(runtime().config_values.AUTH_COOKIE_NAME, auth_token, httponly=True, samesite='lax', secure=runtime().config_values.AUTH_COOKIE_SECURE, max_age=runtime().config_values.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def logout(request: Request):
    identity = session_identity(request)
    if identity is not None:
        revoke_session_token(str(identity['token']))
    sudo_cookie = request.cookies.get(runtime().config_values.SUDO_COOKIE_NAME)
    revoke_sudo_session_token(sudo_cookie if isinstance(sudo_cookie, str) else "")
    response = redirect_response('/login', status_code=303, message='logged out')
    response.delete_cookie(runtime().config_values.AUTH_COOKIE_NAME, path='/', secure=runtime().config_values.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    response.delete_cookie(runtime().config_values.SUDO_COOKIE_NAME, path='/', secure=runtime().config_values.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    return response


def sudo_page(request: Request):
    identity = session_identity(request)
    if identity is None:
        return redirect_response('/login', status_code=303)
    next_path = safe_next_path(request.query_params.get('next'), "/settings")
    if has_sudo_session(request, user_id=int(identity['user_id']), scope=str(_K.SUDO_SCOPE_DESTRUCTIVE)):
        return redirect_response(next_path, status_code=303)
    auth_row = lookup_user_auth(str(identity['username']))
    if auth_row is None:
        return redirect_response('/login', status_code=303, message='user not found')
    password_salt = str(auth_row['password_salt'] or '').strip().lower()
    try:
        password_iters = int(auth_row['password_iters'] or 0)
    except Exception:
        password_iters = 0
    if not _K.HEX_32_RE.fullmatch(password_salt):
        return redirect_response('/login', status_code=303, message='password metadata unavailable')
    if password_iters <= 0:
        return redirect_response('/login', status_code=303, message='password metadata unavailable')
    return template_response(
        request,
        'sudo.html',
        {
            'username': str(identity['username']),
            'next_path': next_path,
            'password_csrf_token': issue_password_form_csrf_token('sudo-password'),
            'password_salt': password_salt,
            'password_iters': password_iters,
        },
    )


def sudo_submit(
    request: Request,
    password: str = Form(''),
    csrf_token: str = Form(''),
    key_id: str = Form(''),
    envelope_token: str = Form(''),
    encrypted_verifier: str = Form(''),
    next: str = Form('/'),
):
    enforce_same_origin_state_change(request)
    _ = password
    identity = session_identity(request)
    if identity is None:
        return redirect_response('/login', status_code=303)
    next_path = safe_next_path(form_text(next), "/settings")
    try:
        password_csrf = form_text(csrf_token).strip()
        if not verify_password_form_csrf_token(password_csrf, 'sudo-password'):
            raise ValueError('invalid password envelope')
        row = lookup_user_auth(str(identity['username']))
        if row is None:
            raise ValueError('invalid password envelope')
        stored_hash = str(row['password_hash'] or '').strip().lower()
        if not _K.HEX_64_RE.fullmatch(stored_hash):
            raise ValueError('invalid password envelope')
        try:
            verifier = password_envelope_store.consume(
                scope='sudo-password',
                purpose='sudo',
                username=str(identity['username']),
                csrf_token=password_csrf,
                key_id=form_text(key_id),
                envelope_token=form_text(envelope_token),
                encrypted_verifier=form_text(encrypted_verifier),
            )
        except ValueError as exc:
            raise ValueError('invalid password envelope') from exc
        expected_hash = password_verifier_storage_hash(verifier)
        if not secrets.compare_digest(expected_hash, stored_hash):
            raise ValueError('invalid password envelope')
        token = create_sudo_session_for_user(int(identity['user_id']), str(_K.SUDO_SCOPE_DESTRUCTIVE))
    except ValueError as exc:
        return redirect_response(f'/sudo?next={quote_plus(next_path)}', status_code=303, message=str(exc))
    response = redirect_response(next_path, status_code=303, message='sudo mode enabled')
    response.set_cookie(
        runtime().config_values.SUDO_COOKIE_NAME,
        token,
        httponly=True,
        samesite='lax',
        secure=runtime().config_values.AUTH_COOKIE_SECURE,
        max_age=int(runtime().config_values.SUDO_COOKIE_MAX_AGE),
        path='/',
    )
    return response

def home(request: Request) -> RedirectResponse:
    user = session_user(request)
    if not user:
        return redirect_response('/login', status_code=303)
    return redirect_response('/problems', status_code=303)
