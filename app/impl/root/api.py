from __future__ import annotations
import secrets
from urllib.parse import quote_plus
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from app.impl.auth.shared import bootstrap_super_admin_with_password_verifier, create_user_with_password_verifier, enforce_same_origin_state_change, has_registered_users, login_rate_limit_check, login_rate_limit_fail, login_rate_limit_key, login_rate_limit_success, lookup_user_auth, normalize_password_iters, normalize_password_salt_hex, normalize_password_verifier_hex, normalize_username_required, password_meta_for_username, redirect_response, safe_next_path, template_response
from app.impl.auth.session import create_session_for_user, create_sudo_session_for_user, has_sudo_session, revoke_session_token, revoke_sudo_session_token, session_identity, session_user
from app.impl.auth.csrf import issue_password_form_csrf_token, password_proof_from_verifier, verify_password_form_csrf_token
from app.impl.runtime.config import config
from app.impl.root.contest_import import (
    _build_contest_import_problem_draft_rows,
    _build_problem_slug_review_rows,
    _contest_slug_review_state,
    _create_contest_import_draft,
    _delete_contest_import_draft,
    _load_contest_import_draft,
    _normalize_import_contest_idx,
    _rollback_imported_contest,
    _resolve_import_contest_slug,
)
from app.impl.run_export.import_source import (
    build_import_slug_hint,
    import_package_as_new_problem,
    import_statement_language_warning,
)
from app.service.importing.contest import PolygonContestImportService

from app.impl.workspace.context_operation import audit, normalize_contest_slug_required, normalize_contest_title_required, user_contests_overview, user_participating_problems
from app.impl.workspace.problem_config import form_text
from app.impl.workspace.context import global_user_ctx

_C = config.constants
_POLYGON_CONTEST_IMPORT_SERVICE = PolygonContestImportService()


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{safe_count} {token}"

def _setup_config_rows() -> list[dict[str, str]]:
    return [
        {'name': 'POLYGONLIKE_DB', 'value': str(config.settings.db_path)},
        {'name': 'POLYGONLIKE_BARE_ROOT', 'value': str(config.settings.bare_root)},
        {'name': 'POLYGONLIKE_WORKSPACE_ROOT', 'value': str(config.settings.workspace_root)},
        {'name': 'POLYGONLIKE_RUN_ROOT', 'value': str(config.settings.run_root)},
        {'name': 'POLYGONLIKE_ARTIFACTS_ROOT', 'value': str(config.settings.artifacts_root)},
        {'name': 'POLYGONLIKE_CACHE_ROOT', 'value': str(config.settings.cache_root)},
    ]


def _render_contest_import_review_page(
    request: Request,
    gctx: dict[str, object],
    draft: dict[str, object],
    *,
    draft_id: str,
    contest_slug_input: str,
    contest_title_input: str,
    problem_slug_overrides: dict[int, str],
    top_error: str = "",
) -> object:
    package_name_obj = draft.get("package_name")
    package_name = package_name_obj.strip() if isinstance(package_name_obj, str) else ""
    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    user_obj = gctx.get("user")
    owner = ""
    if isinstance(user_obj, dict):
        owner_obj = user_obj.get("username")
        if isinstance(owner_obj, str):
            owner = owner_obj.strip()
    rows, rows_have_error = _build_problem_slug_review_rows(owner, draft_rows, problem_slug_overrides)
    slug_state = _contest_slug_review_state(contest_slug_input, package_name)
    slug_input_value = contest_slug_input.strip() if isinstance(contest_slug_input, str) else ""
    if not slug_input_value:
        suggested_slug = slug_state.get("suggested")
        if isinstance(suggested_slug, str):
            slug_input_value = suggested_slug.strip()
    title_input_value = contest_title_input.strip() if isinstance(contest_title_input, str) else ""
    if not title_input_value:
        parsed_title_obj = draft.get("parsed_title")
        if isinstance(parsed_title_obj, str):
            title_input_value = parsed_title_obj.strip()
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    elif bool(slug_state.get("exists")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    has_error = bool(top_error or rows_have_error or contest_slug_error)
    parsed_title_obj = draft.get("parsed_title")
    parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
    return template_response(
        request,
        "contest_import_review.html",
        {
            "user": gctx["user"],
            "default_problem": gctx["default_problem"],
            "active_main": "contests",
            "draft_id": draft_id,
            "package_name": package_name,
            "parsed_title": parsed_title,
            "contest_slug_value": slug_input_value,
            "contest_slug_state": slug_state,
            "contest_slug_error": contest_slug_error,
            "contest_title_value": title_input_value,
            "problem_rows": rows,
            "top_error": str(top_error or "").strip(),
            "has_error": has_error,
        },
    )


def _active_root_user(request: Request | None = None, user: str = "") -> str:
    explicit = str(user or "").strip()
    if explicit:
        return explicit
    if request is None:
        raise HTTPException(status_code=400, detail="missing user context")
    active_user = str(session_user(request) or "").strip()
    if not active_user:
        raise HTTPException(status_code=401, detail="authentication required")
    return active_user

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
        safe_user = raw_user if len(raw_user) <= 64 and _C.USER_IDENT_RE.fullmatch(raw_user) else ''
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

def register_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), next: str=Form('/')):
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
        existing = lookup_user_auth(safe_user)
        if existing is not None:
            raise ValueError('registration failed; username is unavailable')
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
    next_path = safe_next_path(request.query_params.get('next'), f"/problems/{identity['username']}/settings")
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
    next_path = safe_next_path(form_text(next), f"/problems/{identity['username']}/settings")
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

def problems_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    entries = user_participating_problems(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return template_response(request, 'root_problems.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'problems'})


def problems_root_import_slug_hint(request: Request, user: str = "", filename: str = "", requested_slug: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    payload = build_import_slug_hint(str(gctx["user"]["username"]), filename, requested_slug)
    return JSONResponse(payload)


def problems_root_import(request: Request, user: str = "", package_upload: UploadFile | None = File(None), problem_slug: str = Form("")):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    package_name = ""
    package_content: bytes = b""
    try:
        if package_upload is None:
            raise ValueError("package file is required")
        package_name = str(package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("package filename is required")
        package_content = package_upload.file.read()
        imported = import_package_as_new_problem(
            actor_user_id=int(gctx["user"]["id"]),
            actor_user=str(gctx["user"]["username"]),
            package_name=package_name,
            package_content=package_content,
            requested_slug=str(problem_slug or "").strip(),
            source_problem="",
        )
        target_problem_obj = imported.get("target_problem")
        target_problem = target_problem_obj.strip() if isinstance(target_problem_obj, str) else ""
        total_tests_obj = imported.get("total_tests")
        total_tests = int(total_tests_obj) if isinstance(total_tests_obj, int) else 0
        package_format_obj = imported.get("package_format")
        package_format = package_format_obj.strip() if isinstance(package_format_obj, str) else "package"
        msg = f"{package_format} package imported as {target_problem} ({_count_label(total_tests, 'test')})"
        language_warning = import_statement_language_warning(imported)
        if language_warning:
            msg = f"{msg}; warning: {language_warning}"
        return redirect_response(f"/problems/{target_problem}/{gctx['user']['username']}/statement", status_code=303, message=msg)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response("/problems", status_code=303, message=msg)

def contests_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    entries = user_contests_overview(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return template_response(request, 'root_contests.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'contests'})

def contests_root_create(request: Request, user: str = "", contest_slug: str = Form(...), contest_title: str = Form(...)):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    msg = "contest created"
    try:
        slug = normalize_contest_slug_required(contest_slug)
        title = normalize_contest_title_required(contest_title)
        actor_user_id = int(gctx["user"]["id"])
        contest_id = config.contest_service.create_contest_with_owner(
            slug=slug,
            title=title,
            owner_user_id=actor_user_id,
        )
        audit(
            int(gctx["user"]["id"]),
            None,
            "contest.create",
            {
                "contest_id": contest_id,
                "contest_slug": slug,
                "contest_title": title,
                "linked_current_problem": False,
            },
        )
        msg = f"contest {slug} created"
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
    return redirect_response("/contests", status_code=303, message=msg)


def contests_root_import(
    request: Request,
    user: str = "",
    package_upload: UploadFile | None = File(None),
    contest_slug: str = Form(""),
    contest_title: str = Form(""),
):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    package_name = ""
    try:
        if package_upload is None:
            raise ValueError("package file is required")
        package_name = str(package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("package filename is required")
        payload = package_upload.file.read()
        parsed = _POLYGON_CONTEST_IMPORT_SERVICE.parse_package(package_name, payload)
        rows = parsed.get("problems")
        if not isinstance(rows, list) or not rows:
            raise ValueError("contest package has no problems")
        draft_rows = _build_contest_import_problem_draft_rows(actor_username, [dict(item) for item in rows if isinstance(item, dict)])
        if not draft_rows:
            raise ValueError("contest package has no importable problem rows")
        parsed_title_obj = parsed.get("title")
        parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
        draft_id = _create_contest_import_draft(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            package_name=package_name,
            package_payload=payload,
            contest_slug_input=form_text(contest_slug).strip(),
            contest_title_input=form_text(contest_title).strip(),
            parsed_title=parsed_title,
            problem_rows=draft_rows,
        )
        message = f"contest package parsed ({_count_label(len(draft_rows), 'problem')}); review slugs before import"
        return redirect_response(
            f"/contests/import/review?draft_id={quote_plus(draft_id)}",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return redirect_response("/contests", status_code=303, message=message)


def contests_root_import_review(request: Request, user: str = "", draft_id: str = ""):
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    safe_draft_id = str(draft_id or "").strip()
    try:
        draft, _payload_path = _load_contest_import_draft(actor_user_id, actor_username, safe_draft_id)
    except Exception as exc:
        return redirect_response("/contests", status_code=303, message=str(exc))
    return _render_contest_import_review_page(
        request,
        gctx,
        draft,
        draft_id=safe_draft_id,
        contest_slug_input=draft["contest_slug_input"].strip() if isinstance(draft.get("contest_slug_input"), str) else "",
        contest_title_input=draft["contest_title_input"].strip() if isinstance(draft.get("contest_title_input"), str) else "",
        problem_slug_overrides={},
    )


async def contests_root_import_confirm(request: Request, user: str = ""):
    enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    created_contest_slug = ""
    imported_problem_slugs: list[str] = []
    form = await request.form()
    draft_id_obj = form.get("draft_id")
    draft_id = draft_id_obj.strip() if isinstance(draft_id_obj, str) else ""
    contest_slug_obj = form.get("contest_slug")
    contest_slug_input = contest_slug_obj.strip() if isinstance(contest_slug_obj, str) else ""
    contest_title_obj = form.get("contest_title")
    contest_title_input = contest_title_obj.strip() if isinstance(contest_title_obj, str) else ""
    try:
        draft, payload_path = _load_contest_import_draft(actor_user_id, actor_username, draft_id)
    except Exception as exc:
        return redirect_response("/contests", status_code=303, message=str(exc))

    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    problem_slug_overrides: dict[int, str] = {}
    for row in draft_rows:
        seq_obj = row.get("seq")
        seq = int(seq_obj) if isinstance(seq_obj, int) else 0
        if seq <= 0:
            continue
        key = f"problem_slug_{seq}"
        slug_override_obj = form.get(key)
        problem_slug_overrides[seq] = slug_override_obj.strip() if isinstance(slug_override_obj, str) else ""

    review_rows, rows_have_error = _build_problem_slug_review_rows(actor_username, draft_rows, problem_slug_overrides)
    draft_package_name_obj = draft.get("package_name")
    draft_package_name = draft_package_name_obj.strip() if isinstance(draft_package_name_obj, str) else ""
    slug_state = _contest_slug_review_state(contest_slug_input, draft_package_name)
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    elif bool(slug_state.get("exists")):
        message_obj = slug_state.get("message")
        contest_slug_error = message_obj if isinstance(message_obj, str) else ""
    if rows_have_error or contest_slug_error:
        return _render_contest_import_review_page(
            request,
            gctx,
            draft,
            draft_id=draft_id,
            contest_slug_input=contest_slug_input,
            contest_title_input=contest_title_input,
            problem_slug_overrides=problem_slug_overrides,
        )

    package_name = draft_package_name
    try:
        payload = payload_path.read_bytes()
        parsed = _POLYGON_CONTEST_IMPORT_SERVICE.parse_package(package_name, payload)
        parsed_rows_raw = parsed.get("problems")
        parsed_rows = [dict(item) for item in parsed_rows_raw] if isinstance(parsed_rows_raw, list) else []
        statement_files_raw = parsed.get("statement_files")
        statement_files = [dict(item) for item in statement_files_raw] if isinstance(statement_files_raw, list) else []
        default_language_obj = parsed.get("default_language")
        default_language = default_language_obj.strip().lower() if isinstance(default_language_obj, str) else ""
        location_obj = parsed.get("location")
        inferred_location = location_obj.strip() if isinstance(location_obj, str) else ""
        date_obj = parsed.get("date")
        inferred_date = date_obj.strip() if isinstance(date_obj, str) else ""
        if len(parsed_rows) != len(review_rows):
            raise ValueError("contest package changed; please re-upload and review again")

        target_contest_slug = _resolve_import_contest_slug(contest_slug_input, package_name)
        parsed_title_obj = parsed.get("title")
        parsed_title = parsed_title_obj.strip() if isinstance(parsed_title_obj, str) else ""
        target_contest_title = normalize_contest_title_required(
            form_text(contest_title_input).strip() or parsed_title or target_contest_slug
        )

        contest_id = config.contest_service.create_contest_with_owner(
            slug=target_contest_slug,
            title=target_contest_title,
            owner_user_id=actor_user_id,
        )
        created_contest_slug = target_contest_slug

        import_warnings: list[str] = []
        used_indices: set[str] = set()
        source_folder_map: dict[int, str] = {}
        for idx, row in enumerate(parsed_rows, start=1):
            row_review = review_rows[idx - 1]
            sub_package_name_obj = row.get("package_name")
            sub_package_name = sub_package_name_obj.strip() if isinstance(sub_package_name_obj, str) else ""
            if not sub_package_name:
                sub_package_name = f"problem-{idx}.zip"
            package_bytes_obj = row.get("package_bytes")
            sub_package_bytes = bytes(package_bytes_obj) if isinstance(package_bytes_obj, (bytes, bytearray)) else b""
            if not sub_package_bytes:
                raise ValueError(f"empty problem package payload for #{idx}")
            requested_problem_slug_obj = row_review.get("slug_input")
            requested_problem_slug = requested_problem_slug_obj.strip().lower() if isinstance(requested_problem_slug_obj, str) else ""
            imported = import_package_as_new_problem(
                actor_user_id=actor_user_id,
                actor_user=actor_username,
                package_name=sub_package_name,
                package_content=sub_package_bytes,
                requested_slug=requested_problem_slug,
                source_problem="",
                normalize_test_data_newlines=True,
                build_polygon_sample_answers=False,
            )
            imported_problem_slug_obj = imported.get("target_problem")
            imported_problem_slug = imported_problem_slug_obj.strip() if isinstance(imported_problem_slug_obj, str) else ""
            if not imported_problem_slug:
                raise RuntimeError(f"failed to import problem package #{idx}")
            language_warning = import_statement_language_warning(imported)
            if language_warning:
                import_warnings.append(f"{imported_problem_slug}: {language_warning}")
            problem_id = config.workspace_service.known_problem_id(imported_problem_slug)
            if problem_id is None:
                raise RuntimeError(f"imported problem missing: {imported_problem_slug}")
            source_folder_obj = row.get("source_folder")
            source_folder = source_folder_obj.strip() if isinstance(source_folder_obj, str) else ""
            if not source_folder:
                raise RuntimeError(f"contest source folder missing for problem #{idx}")
            contest_problem_idx = _normalize_import_contest_idx(row.get("index"), idx, used_indices)
            config.contest_service.add_problem(
                contest_id=contest_id,
                idx=contest_problem_idx,
                problem_id=problem_id,
                added_by_user_id=actor_user_id,
            )
            source_folder_map[int(problem_id)] = source_folder
            imported_problem_slugs.append(imported_problem_slug)

        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=target_contest_slug,
            actor_user_id=actor_user_id,
            files=statement_files,
        )
        config.contest_service.set_statement_default_language(contest_id, actor_user_id, default_language)
        if inferred_location:
            config.contest_service.upsert_property(contest_id, actor_user_id, "location", inferred_location)
        if inferred_date:
            config.contest_service.upsert_property(contest_id, actor_user_id, "date", inferred_date)
        config.contest_service.set_statement_problem_source_folders(contest_id, actor_user_id, source_folder_map)

        audit(
            actor_user_id,
            None,
            "contest.import",
            {
                "contest_id": contest_id,
                "contest_slug": target_contest_slug,
                "contest_title": target_contest_title,
                "package": package_name,
                "draft_id": draft_id,
                "problems_imported": imported_problem_slugs,
                "total_problems": len(imported_problem_slugs),
                "statement_default_language": default_language,
                "location": inferred_location,
                "date": inferred_date,
                "normalize_test_data_newlines": True,
            },
        )
        _delete_contest_import_draft(draft_id)
        message = f"contest {target_contest_slug} imported ({_count_label(len(imported_problem_slugs), 'problem')})"
        if import_warnings:
            first_warning = str(import_warnings[0] or "").strip()
            extra = max(0, len(import_warnings) - 1)
            suffix = f" (+{extra} more)" if extra > 0 else ""
            message = f"{message}; warning: {first_warning}{suffix}"
        return redirect_response(
            f"/contests/{target_contest_slug}/{actor_username}/overview",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    if created_contest_slug or imported_problem_slugs:
        try:
            _rollback_imported_contest(created_contest_slug, imported_problem_slugs)
        finally:
            _delete_contest_import_draft(draft_id)
        return redirect_response("/contests", status_code=303, message=message)
    return _render_contest_import_review_page(
        request,
        gctx,
        draft,
        draft_id=draft_id,
        contest_slug_input=contest_slug_input,
        contest_title_input=contest_title_input,
        problem_slug_overrides=problem_slug_overrides,
        top_error=message,
    )


