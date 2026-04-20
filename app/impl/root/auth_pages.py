from __future__ import annotations

import secrets

from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from urllib.parse import quote_plus

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
    normalize_password_verifier_hex,
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
    password_proof_from_verifier,
    verify_password_form_csrf_token,
)
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.main_util import form_text

_C = config.constants


def _setup_config_rows() -> list[dict[str, str]]:
    return [
        {'name': 'POLYGON_REPLICA_DB', 'value': str(config.settings.db_path)},
        {'name': 'POLYGON_REPLICA_BARE_ROOT', 'value': str(config.settings.bare_root)},
        {'name': 'POLYGON_REPLICA_WORKSPACE_ROOT', 'value': str(config.settings.workspace_root)},
        {'name': 'POLYGON_REPLICA_ARTIFACTS_ROOT', 'value': str(config.settings.artifacts_root)},
        {'name': 'POLYGON_REPLICA_CACHE_ROOT', 'value': str(config.settings.cache_root)},
    ]


def setup_page(request: Request):
    user = session_user(request)
    next_path = safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register', '/setup'} else '/problems'
        return redirect_response(target, status_code=303)
    if has_registered_users():
        return redirect_response('/login', status_code=303, message='setup already completed')
    return template_response(request, 'setup.html', {'next_path': next_path, 'password_csrf_token': issue_password_form_csrf_token('setup-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS), 'config_rows': _setup_config_rows()})

def setup_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), confirm_config: str=Form('0'), next: str=Form('/')):
    enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    try:
        if has_registered_users():
            raise ValueError('setup already completed')
        if str(confirm_config or '').strip() not in {'1', 'true', 'on', 'yes'}:
            raise ValueError('please confirm current system configuration paths')
        safe_user = normalize_username_required(form_text(username))
        proof_token = form_text(csrf_token).strip()
        proof_value = form_text(password_proof).strip().lower()
        verifier_value = form_text(password_verifier).strip().lower()
        salt_value = form_text(password_salt)
        iter_value = form_text(password_iters)
        next_path = form_text(next)
        if not verify_password_form_csrf_token(proof_token, 'setup-password'):
            raise ValueError('setup failed; invalid csrf token')
        verifier = normalize_password_verifier_hex(verifier_value)
        if not _C.HEX_64_RE.fullmatch(proof_value):
            raise ValueError('setup failed; invalid password proof')
        salt_hex = normalize_password_salt_hex(salt_value)
        iters = normalize_password_iters(iter_value)
        if iters != int(_C.PASSWORD_HASH_ITERS):
            raise ValueError('setup failed; invalid password iterations')
        if not secrets.compare_digest(password_proof_from_verifier(proof_token, verifier), proof_value):
            raise ValueError('setup failed; invalid password proof')
        user_id = bootstrap_super_admin_with_password_verifier(safe_user, verifier, salt_hex, iters)
        token = create_session_for_user(int(user_id))
        audit(int(user_id), None, 'system.setup', {'super_admin': safe_user, 'config_confirmed': True})
    except ValueError as exc:
        return redirect_response('/setup', status_code=303, message=str(exc))
    target = safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register', '/setup'}:
        target = '/problems'
    response = redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
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

def login_submit(request: Request, username: str=Form(...), password: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), next: str=Form('/')):
    enforce_same_origin_state_change(request)
    raw_user = form_text(username).strip()
    proof_token = form_text(csrf_token).strip()
    proof_value = form_text(password_proof).strip().lower()
    next_path = form_text(next)
    rate_limit_key = login_rate_limit_key(raw_user, request)
    try:
        login_rate_limit_check(rate_limit_key)
        safe_user = (
            raw_user
            if _C.USERNAME_MIN_LEN <= len(raw_user) <= _C.USERNAME_MAX_LEN and _C.USER_IDENT_RE.fullmatch(raw_user)
            else ''
        )
        if not safe_user:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        row = lookup_user_auth(safe_user)
        if row is None:
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        if not verify_password_form_csrf_token(proof_token, 'login-password'):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        verifier = str(row['password_hash'] or '').strip().lower()
        if not _C.HEX_64_RE.fullmatch(verifier):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        if not _C.HEX_64_RE.fullmatch(proof_value):
            login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        expected_proof = password_proof_from_verifier(proof_token, verifier)
        if not secrets.compare_digest(expected_proof, proof_value):
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
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def register_page(request: Request):
    user = session_user(request)
    next_path = safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else '/problems'
        return redirect_response(target, status_code=303)
    return template_response(request, 'register.html', {'next_path': next_path, 'password_csrf_token': issue_password_form_csrf_token('register-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS)})

def register_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), next: str=Form('/'), terms_accepted: str=Form('')):
    enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    try:
        safe_user = normalize_username_required(form_text(username))
        proof_token = form_text(csrf_token).strip()
        proof_value = form_text(password_proof).strip().lower()
        verifier_value = form_text(password_verifier).strip().lower()
        salt_value = form_text(password_salt)
        iter_value = form_text(password_iters)
        next_path = form_text(next)
        terms_value = form_text(terms_accepted)
        existing = lookup_user_auth(safe_user)
        if existing is not None:
            raise ValueError('registration failed; username is unavailable')
        if terms_value != 'yes':
            raise ValueError('registration failed; terms of use must be accepted')
        if not verify_password_form_csrf_token(proof_token, 'register-password'):
            raise ValueError('registration failed; invalid csrf token')
        verifier = normalize_password_verifier_hex(verifier_value)
        if not _C.HEX_64_RE.fullmatch(proof_value):
            raise ValueError('registration failed; invalid password proof')
        salt_hex = normalize_password_salt_hex(salt_value)
        iters = normalize_password_iters(iter_value)
        if iters != int(_C.PASSWORD_HASH_ITERS):
            raise ValueError('registration failed; invalid password iterations')
        expected_proof = password_proof_from_verifier(proof_token, verifier)
        if not secrets.compare_digest(expected_proof, proof_value):
            raise ValueError('registration failed; invalid password proof')
        user_id = create_user_with_password_verifier(safe_user, verifier, salt_hex, iters)
        token = create_session_for_user(int(user_id))
    except ValueError as exc:
        return redirect_response('/register', status_code=303, message=str(exc))
    target = safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register'}:
        target = '/problems'
    response = redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def logout(request: Request):
    identity = session_identity(request)
    if identity is not None:
        revoke_session_token(str(identity['token']))
    sudo_cookie = request.cookies.get(_C.SUDO_COOKIE_NAME)
    revoke_sudo_session_token(sudo_cookie if isinstance(sudo_cookie, str) else "")
    response = redirect_response('/login', status_code=303, message='logged out')
    response.delete_cookie(_C.AUTH_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    response.delete_cookie(_C.SUDO_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    return response


def sudo_page(request: Request):
    identity = session_identity(request)
    if identity is None:
        return redirect_response('/login', status_code=303)
    next_path = safe_next_path(request.query_params.get('next'), "/settings")
    if has_sudo_session(request, user_id=int(identity['user_id']), scope=str(_C.SUDO_SCOPE_DESTRUCTIVE)):
        return redirect_response(next_path, status_code=303)
    auth_row = lookup_user_auth(str(identity['username']))
    if auth_row is None:
        return redirect_response('/login', status_code=303, message='user not found')
    password_salt = str(auth_row['password_salt'] or '').strip().lower()
    try:
        password_iters = int(auth_row['password_iters'] or 0)
    except Exception:
        password_iters = 0
    if not _C.HEX_32_RE.fullmatch(password_salt):
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


def sudo_submit(request: Request, password: str = Form(''), password_proof: str = Form(''), csrf_token: str = Form(''), next: str = Form('/')):
    enforce_same_origin_state_change(request)
    identity = session_identity(request)
    if identity is None:
        return redirect_response('/login', status_code=303)
    next_path = safe_next_path(form_text(next), "/settings")
    try:
        proof_token = form_text(csrf_token).strip()
        proof_value = form_text(password_proof).strip().lower()
        if not verify_password_form_csrf_token(proof_token, 'sudo-password'):
            raise ValueError('invalid password proof')
        row = lookup_user_auth(str(identity['username']))
        if row is None:
            raise ValueError('invalid password proof')
        verifier = str(row['password_hash'] or '').strip().lower()
        if not _C.HEX_64_RE.fullmatch(verifier):
            raise ValueError('invalid password proof')
        if not _C.HEX_64_RE.fullmatch(proof_value):
            raise ValueError('invalid password proof')
        expected_proof = password_proof_from_verifier(proof_token, verifier)
        if not secrets.compare_digest(expected_proof, proof_value):
            raise ValueError('invalid password proof')
        token = create_sudo_session_for_user(int(identity['user_id']), str(_C.SUDO_SCOPE_DESTRUCTIVE))
    except ValueError as exc:
        return redirect_response(f'/sudo?next={quote_plus(next_path)}', status_code=303, message=str(exc))
    response = redirect_response(next_path, status_code=303, message='sudo mode enabled')
    response.set_cookie(
        _C.SUDO_COOKIE_NAME,
        token,
        httponly=True,
        samesite='lax',
        secure=_C.AUTH_COOKIE_SECURE,
        max_age=int(_C.SUDO_COOKIE_MAX_AGE),
        path='/',
    )
    return response

def home(request: Request) -> RedirectResponse:
    user = session_user(request)
    if not user:
        return redirect_response('/login', status_code=303)
    return redirect_response('/problems', status_code=303)
