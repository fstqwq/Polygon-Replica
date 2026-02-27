from __future__ import annotations
import secrets
from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from app.impl.auth import (
    _bootstrap_super_admin_with_password,
    _bootstrap_super_admin_with_password_verifier,
    _create_session_for_user,
    _create_user_with_password,
    _create_user_with_password_verifier,
    _enforce_same_origin_state_change,
    _has_registered_users,
    _issue_password_form_csrf_token,
    _login_rate_limit_check,
    _login_rate_limit_fail,
    _login_rate_limit_key,
    _login_rate_limit_success,
    _lookup_user_auth,
    _normalize_password_iters,
    _normalize_password_salt_hex,
    _normalize_password_verifier_hex,
    _normalize_username_required,
    _password_meta_for_username,
    _password_proof_from_verifier,
    _redirect_response,
    _revoke_session_token,
    _safe_next_path,
    _session_identity,
    _session_user,
    _template_response,
    _validate_password,
    _verify_password_form_csrf_token,
    _verify_user_password,
)
from app.impl.config import config
from app.db import now_iso

from app.impl.workspace import (
    _audit,
    _form_text,
    _global_user_ctx,
    _normalize_contest_slug_required,
    _normalize_contest_title_required,
    _user_contests_overview,
    _user_participating_problems,
)

_C = config.constants

def _setup_config_rows() -> list[dict[str, str]]:
    return [
        {'name': 'POLYGONLIKE_DB', 'value': str(config.settings.db_path)},
        {'name': 'POLYGONLIKE_BARE_ROOT', 'value': str(config.settings.bare_root)},
        {'name': 'POLYGONLIKE_WORKSPACE_ROOT', 'value': str(config.settings.workspace_root)},
        {'name': 'POLYGONLIKE_RUN_ROOT', 'value': str(config.settings.run_root)},
        {'name': 'POLYGONLIKE_ARTIFACTS_ROOT', 'value': str(config.settings.artifacts_root)},
        {'name': 'POLYGONLIKE_CACHE_ROOT', 'value': str(config.settings.cache_root)},
    ]

def setup_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register', '/setup'} else f'/problems/{user}/problems'
        return _redirect_response(target, status_code=303)
    if _has_registered_users():
        return _redirect_response('/login', status_code=303, message='setup already completed')
    return _template_response(request, 'setup.html', {'next_path': next_path, 'password_csrf_token': _issue_password_form_csrf_token('setup-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS), 'config_rows': _setup_config_rows()})

def setup_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), confirm_config: str=Form('0'), next: str=Form('/')):
    _enforce_same_origin_state_change(request)
    try:
        if _has_registered_users():
            raise ValueError('setup already completed')
        if str(confirm_config or '').strip() not in {'1', 'true', 'on', 'yes'}:
            raise ValueError('please confirm current system configuration paths')
        safe_user = _normalize_username_required(_form_text(username))
        raw_password = _form_text(password)
        raw_password_confirm = _form_text(password_confirm)
        proof_token = _form_text(csrf_token).strip()
        proof_value = _form_text(password_proof).strip().lower()
        verifier_value = _form_text(password_verifier).strip().lower()
        salt_value = _form_text(password_salt)
        iter_value = _form_text(password_iters)
        next_path = _form_text(next)
        verifier_mode = bool(proof_token or proof_value or verifier_value)
        if verifier_mode:
            if not _verify_password_form_csrf_token(proof_token, 'setup-password'):
                raise ValueError('setup failed; invalid csrf token')
            verifier = _normalize_password_verifier_hex(verifier_value)
            if not _C.HEX_64_RE.fullmatch(proof_value):
                raise ValueError('setup failed; invalid password proof')
            salt_hex = _normalize_password_salt_hex(salt_value)
            iters = _normalize_password_iters(iter_value)
            if iters != int(_C.PASSWORD_HASH_ITERS):
                raise ValueError('setup failed; invalid password iterations')
            expected_proof = _password_proof_from_verifier(proof_token, verifier)
            if not secrets.compare_digest(expected_proof, proof_value):
                raise ValueError('setup failed; invalid password proof')
            user_id = _bootstrap_super_admin_with_password_verifier(safe_user, verifier, salt_hex, iters)
        else:
            if raw_password != raw_password_confirm:
                raise ValueError('password confirmation does not match')
            _validate_password(raw_password)
            user_id = _bootstrap_super_admin_with_password(safe_user, raw_password)
        token = _create_session_for_user(int(user_id))
        _audit(int(user_id), None, 'system.setup', {'super_admin': safe_user, 'config_confirmed': True})
    except ValueError as exc:
        return _redirect_response('/setup', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, f'/problems/{safe_user}/problems')
    if target in {'/', '/login', '/register', '/setup'}:
        target = f'/problems/{safe_user}/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def login_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else f'/problems/{user}/problems'
        return _redirect_response(target, status_code=303)
    return _template_response(request, 'login.html', {'next_path': next_path, 'password_csrf_token': _issue_password_form_csrf_token('login-password')})

def auth_password_meta(username: str='', csrf_token: str=''):
    if not _verify_password_form_csrf_token(csrf_token, 'login-password'):
        raise HTTPException(status_code=400, detail='invalid csrf token')
    salt_hex, iterations = _password_meta_for_username(username)
    return {'salt': salt_hex, 'iters': iterations}

def login_submit(request: Request, username: str=Form(...), password: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), next: str=Form('/')):
    _enforce_same_origin_state_change(request)
    raw_user = _form_text(username).strip()
    raw_password = _form_text(password)
    proof_token = _form_text(csrf_token).strip()
    proof_value = _form_text(password_proof).strip().lower()
    next_path = _form_text(next)
    rate_limit_key = _login_rate_limit_key(raw_user, request)
    try:
        _login_rate_limit_check(rate_limit_key)
        safe_user = raw_user if len(raw_user) <= 64 and _C.USER_IDENT_RE.fullmatch(raw_user) else ''
        if not safe_user:
            _login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        row = _lookup_user_auth(safe_user)
        if row is None:
            _login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        proof_mode = bool(proof_token or proof_value)
        if proof_mode:
            if not _verify_password_form_csrf_token(proof_token, 'login-password'):
                _login_rate_limit_fail(rate_limit_key)
                raise ValueError('invalid username or password')
            verifier = str(row['password_hash'] or '').strip().lower()
            if not _C.HEX_64_RE.fullmatch(verifier):
                _login_rate_limit_fail(rate_limit_key)
                raise ValueError('invalid username or password')
            if not _C.HEX_64_RE.fullmatch(proof_value):
                _login_rate_limit_fail(rate_limit_key)
                raise ValueError('invalid username or password')
            expected_proof = _password_proof_from_verifier(proof_token, verifier)
            if not secrets.compare_digest(expected_proof, proof_value):
                _login_rate_limit_fail(rate_limit_key)
                raise ValueError('invalid username or password')
        elif not _verify_user_password(row, raw_password):
            _login_rate_limit_fail(rate_limit_key)
            raise ValueError('invalid username or password')
        _login_rate_limit_success(rate_limit_key)
        token = _create_session_for_user(int(row['id']))
    except ValueError as exc:
        return _redirect_response('/login', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, f'/problems/{safe_user}/problems')
    if target in {'/', '/login', '/register'}:
        target = f'/problems/{safe_user}/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def register_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else f'/problems/{user}/problems'
        return _redirect_response(target, status_code=303)
    return _template_response(request, 'register.html', {'next_path': next_path, 'password_csrf_token': _issue_password_form_csrf_token('register-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS)})

def register_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), next: str=Form('/')):
    _enforce_same_origin_state_change(request)
    try:
        safe_user = _normalize_username_required(_form_text(username))
        raw_password = _form_text(password)
        raw_password_confirm = _form_text(password_confirm)
        proof_token = _form_text(csrf_token).strip()
        proof_value = _form_text(password_proof).strip().lower()
        verifier_value = _form_text(password_verifier).strip().lower()
        salt_value = _form_text(password_salt)
        iter_value = _form_text(password_iters)
        next_path = _form_text(next)
        existing = _lookup_user_auth(safe_user)
        if existing is not None:
            raise ValueError('registration failed; username is unavailable')
        verifier_mode = bool(proof_token or proof_value or verifier_value)
        if verifier_mode:
            if not _verify_password_form_csrf_token(proof_token, 'register-password'):
                raise ValueError('registration failed; invalid csrf token')
            verifier = _normalize_password_verifier_hex(verifier_value)
            if not _C.HEX_64_RE.fullmatch(proof_value):
                raise ValueError('registration failed; invalid password proof')
            salt_hex = _normalize_password_salt_hex(salt_value)
            iters = _normalize_password_iters(iter_value)
            if iters != int(_C.PASSWORD_HASH_ITERS):
                raise ValueError('registration failed; invalid password iterations')
            expected_proof = _password_proof_from_verifier(proof_token, verifier)
            if not secrets.compare_digest(expected_proof, proof_value):
                raise ValueError('registration failed; invalid password proof')
            user_id = _create_user_with_password_verifier(safe_user, verifier, salt_hex, iters)
        else:
            if raw_password != raw_password_confirm:
                raise ValueError('password confirmation does not match')
            _validate_password(raw_password)
            user_id = _create_user_with_password(safe_user, raw_password)
        token = _create_session_for_user(int(user_id))
    except ValueError as exc:
        return _redirect_response('/register', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, f'/problems/{safe_user}/problems')
    if target in {'/', '/login', '/register'}:
        target = f'/problems/{safe_user}/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def logout(request: Request):
    identity = _session_identity(request)
    if identity is not None:
        _revoke_session_token(str(identity['token']))
    response = _redirect_response('/login', status_code=303, message='logged out')
    response.delete_cookie(_C.AUTH_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    return response

def home(request: Request) -> RedirectResponse:
    user = _session_user(request)
    if not user:
        return _redirect_response('/login', status_code=303)
    return _redirect_response(f'/problems/{user}/problems', status_code=303)

def problems_root_page(request: Request, user: str):
    gctx = _global_user_ctx(user)
    entries = _user_participating_problems(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return _template_response(request, 'root_problems.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'problems'})

def contests_root_page(request: Request, user: str):
    gctx = _global_user_ctx(user)
    entries = _user_contests_overview(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return _template_response(request, 'root_contests.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'contests'})

def contests_root_create(user: str, contest_slug: str=Form(...), contest_title: str=Form(...)):
    gctx = _global_user_ctx(user)
    msg = 'contest created'
    try:
        slug = _normalize_contest_slug_required(contest_slug)
        title = _normalize_contest_title_required(contest_title)
        exists = config.db.fetch_one('SELECT id FROM contests WHERE slug=?', [slug])
        if exists is not None:
            raise ValueError('contest slug already exists')
        config.db.execute('INSERT INTO contests(slug,title,owner_user_id,created_at) VALUES(?,?,?,?)', [slug, title, int(gctx['user']['id']), now_iso()])
        contest_row = config.db.fetch_one('SELECT id FROM contests WHERE slug=?', [slug])
        if contest_row is None:
            raise RuntimeError('failed to create contest')
        contest_id = int(contest_row['id'])
        config.db.execute('INSERT OR IGNORE INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)', [contest_id, int(gctx['user']['id']), 'owner', now_iso()])
        _audit(int(gctx['user']['id']), None, 'contest.create', {'contest_id': contest_id, 'contest_slug': slug, 'contest_title': title, 'linked_current_problem': False})
        msg = f'contest {slug} created'
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
    return _redirect_response(f'/problems/{user}/contests', status_code=303, message=msg)
