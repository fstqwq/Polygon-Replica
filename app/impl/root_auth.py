from __future__ import annotations
import json
import sqlite3
import secrets
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from app.impl.auth import (
    _create_sudo_session_for_user,
    _bootstrap_super_admin_with_password_verifier,
    _create_session_for_user,
    _create_user_with_password_verifier,
    _enforce_same_origin_state_change,
    _has_sudo_session,
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
    _revoke_sudo_session_token,
    _revoke_session_token,
    _safe_next_path,
    _session_identity,
    _session_user,
    _template_response,
    _verify_password_form_csrf_token,
)
from app.impl.config import config
from app.impl import run_export as run_export_impl
from app.db import now_iso
from app.services.polygon_contest_import_service import PolygonContestImportService

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
_POLYGON_CONTEST_IMPORT_SERVICE = PolygonContestImportService()
_CONTEST_IMPORT_SUFFIX_RE = re.compile(r"-\d+$")
_CONTEST_IMPORT_DRAFT_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_CONTEST_IMPORT_DRAFT_TTL_SEC = 6 * 60 * 60
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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


def _slugify_contest_id(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


def _import_contest_slug_base_from_package_name(package_name: str) -> str:
    raw_stem = str(Path(str(package_name or "imported-contest.zip")).stem or "").strip()
    normalized_stem = _CONTEST_IMPORT_SUFFIX_RE.sub("", raw_stem).strip()
    if not normalized_stem:
        normalized_stem = raw_stem
    slug = _slugify_contest_id(normalized_stem)
    base = slug or "imported-contest"
    if not _C.CONTEST_IDENT_RE.fullmatch(base):
        return "imported-contest"
    return base


def _next_available_contest_slug(base: str) -> str:
    token = str(base or "").strip() or "imported-contest"
    candidate = token
    idx = 2
    while config.db.fetch_one("SELECT id FROM contests WHERE slug=?", [candidate]) is not None:
        suffix = f"-{idx}"
        prefix_len = max(1, 64 - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "c"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate


def _resolve_import_contest_slug(requested_slug: str, package_name: str) -> str:
    requested = str(requested_slug or "").strip()
    if requested:
        slug = _normalize_contest_slug_required(requested)
        exists = config.db.fetch_one("SELECT id FROM contests WHERE slug=?", [slug])
        if exists is not None:
            suggestion = _next_available_contest_slug(slug)
            raise ValueError(f"contest slug already exists: {slug} (try: {suggestion})")
        return slug
    base = _import_contest_slug_base_from_package_name(package_name)
    return _next_available_contest_slug(base)


def _contest_idx_label(seq: int) -> str:
    value = max(1, int(seq))
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def _normalize_import_contest_idx(raw: object, seq: int, used: set[str]) -> str:
    token = str(raw or "").strip().upper()
    if token and len(token) <= 16 and _C.CONTEST_IDENT_RE.fullmatch(token) and token not in used:
        used.add(token)
        return token
    candidate_seq = max(1, int(seq))
    while True:
        candidate = _contest_idx_label(candidate_seq)
        if candidate not in used:
            used.add(candidate)
            return candidate
        candidate_seq += 1


def _contest_import_draft_root() -> Path:
    root = (config.settings.cache_root / "contest-import-drafts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_contest_import_draft_id(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not _CONTEST_IMPORT_DRAFT_ID_RE.fullmatch(token):
        raise ValueError("invalid contest import draft id")
    return token


def _contest_import_draft_paths(draft_id: str) -> tuple[Path, Path]:
    safe_id = _safe_contest_import_draft_id(draft_id)
    root = _contest_import_draft_root()
    meta_path = (root / f"{safe_id}.json").resolve()
    payload_path = (root / f"{safe_id}.zip").resolve()
    if root not in meta_path.parents or root not in payload_path.parents:
        raise ValueError("invalid contest import draft path")
    return meta_path, payload_path


def _cleanup_stale_contest_import_drafts() -> None:
    root = _contest_import_draft_root()
    deadline = time.time() - float(_CONTEST_IMPORT_DRAFT_TTL_SEC)
    try:
        for meta in root.glob("*.json"):
            if meta.is_symlink() or (not meta.is_file()):
                continue
            stem = str(meta.stem or "").strip().lower()
            if not _CONTEST_IMPORT_DRAFT_ID_RE.fullmatch(stem):
                continue
            try:
                st = meta.stat()
            except OSError:
                continue
            if float(st.st_mtime) >= deadline:
                continue
            _, payload = _contest_import_draft_paths(stem)
            meta.unlink(missing_ok=True)
            payload.unlink(missing_ok=True)
    except OSError:
        return


def _slugify_problem_id(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if len(token) > 64:
        token = token[:64].rstrip("-")
    return token


def _normalize_problem_slug_segment_required(raw: str) -> str:
    token = _slugify_problem_id(raw)
    if not token or (not _PROBLEM_SEGMENT_RE.fullmatch(token)):
        raise ValueError(_C.PROBLEM_ID_RULE_MESSAGE)
    return token


def _problem_full_slug(owner: str, slug_segment: str) -> str:
    safe_owner = str(owner or "").strip().lower()
    if not _C.USER_IDENT_RE.fullmatch(safe_owner):
        raise ValueError(_C.USERNAME_RULE_MESSAGE)
    safe_segment = _normalize_problem_slug_segment_required(slug_segment)
    return f"{safe_owner}/{safe_segment}"


def _next_available_problem_slug(owner: str, base: str, reserved: set[str] | None = None) -> str:
    token = str(base or "").strip().lower()
    token = _slugify_problem_id(token)
    if not token:
        token = "imported-problem"
    if not _PROBLEM_SEGMENT_RE.fullmatch(token):
        token = "imported-problem"
    seen = set(reserved or set())
    candidate = token
    idx = 2
    while (candidate in seen) or (config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [_problem_full_slug(owner, candidate)]) is not None):
        suffix = f"-{idx}"
        prefix_len = max(1, 64 - len(suffix))
        prefix = token[:prefix_len].rstrip("-") or "p"
        candidate = f"{prefix}{suffix}"
        idx += 1
    return candidate


def _build_contest_import_problem_draft_rows(owner: str, parsed_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    reserved: set[str] = set()
    for seq, raw in enumerate(parsed_rows, start=1):
        row = dict(raw) if isinstance(raw, dict) else {}
        source_slug = _slugify_problem_id(str(row.get("source_slug") or "")) or f"problem-{seq}"
        package_name = str(row.get("package_name") or "").strip() or f"{source_slug}.zip"
        index = str(row.get("index") or "").strip().upper() or _contest_idx_label(seq)
        suggested = _next_available_problem_slug(owner, source_slug, reserved=reserved)
        reserved.add(suggested)
        rows.append(
            {
                "seq": seq,
                "index": index,
                "source_slug": source_slug,
                "package_name": package_name,
                "suggested_slug": suggested,
            }
        )
    return rows


def _create_contest_import_draft(
    *,
    actor_user_id: int,
    actor_username: str,
    package_name: str,
    package_payload: bytes,
    contest_slug_input: str,
    contest_title_input: str,
    parsed_title: str,
    problem_rows: list[dict[str, object]],
) -> str:
    _cleanup_stale_contest_import_drafts()
    draft_id = uuid.uuid4().hex[:24]
    meta_path, payload_path = _contest_import_draft_paths(draft_id)
    payload_path.write_bytes(bytes(package_payload or b""))
    payload_stat = payload_path.stat()
    meta = {
        "draft_id": draft_id,
        "actor_user_id": int(actor_user_id),
        "actor_username": str(actor_username or "").strip(),
        "package_name": str(package_name or "").strip(),
        "package_size": int(payload_stat.st_size),
        "contest_slug_input": str(contest_slug_input or "").strip(),
        "contest_title_input": str(contest_title_input or "").strip(),
        "parsed_title": str(parsed_title or "").strip(),
        "problem_rows": [dict(row) for row in problem_rows],
        "created_at": now_iso(),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return draft_id


def _load_contest_import_draft(actor_user_id: int, actor_username: str, draft_id: str) -> tuple[dict[str, object], Path]:
    meta_path, payload_path = _contest_import_draft_paths(draft_id)
    if not meta_path.exists() or not meta_path.is_file() or meta_path.is_symlink():
        raise ValueError("contest import draft not found")
    if not payload_path.exists() or not payload_path.is_file() or payload_path.is_symlink():
        raise ValueError("contest import payload not found")
    try:
        meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid contest import draft metadata: {exc}") from exc
    if not isinstance(meta_raw, dict):
        raise ValueError("invalid contest import draft metadata")
    owner_id = int(meta_raw.get("actor_user_id") or 0)
    owner_name = str(meta_raw.get("actor_username") or "").strip()
    if owner_id != int(actor_user_id) or owner_name != str(actor_username or "").strip():
        raise ValueError("contest import draft owner mismatch")
    return dict(meta_raw), payload_path


def _delete_contest_import_draft(draft_id: str) -> None:
    try:
        meta_path, payload_path = _contest_import_draft_paths(draft_id)
    except ValueError:
        return
    meta_path.unlink(missing_ok=True)
    payload_path.unlink(missing_ok=True)


def _build_problem_slug_review_rows(
    owner: str,
    draft_rows: list[dict[str, object]],
    requested_overrides: dict[int, str],
) -> tuple[list[dict[str, object]], bool]:
    rows: list[dict[str, object]] = []
    requested_tokens: list[str] = []
    for row in draft_rows:
        seq = int(row.get("seq") or 0)
        fallback = str(row.get("suggested_slug") or "").strip()
        requested = _slugify_problem_id(str(requested_overrides.get(seq, fallback) or "").strip().lower())
        requested_tokens.append(requested)
    duplicate_counts: dict[str, int] = {}
    for token in requested_tokens:
        if not token:
            continue
        duplicate_counts[token] = int(duplicate_counts.get(token, 0)) + 1
    has_error = False
    for idx, row in enumerate(draft_rows):
        seq = int(row.get("seq") or 0)
        requested = requested_tokens[idx]
        valid = bool(requested and _PROBLEM_SEGMENT_RE.fullmatch(requested))
        full_requested = _problem_full_slug(owner, requested) if valid else ""
        exists = bool(full_requested and (config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [full_requested]) is not None))
        duplicate = bool(requested and int(duplicate_counts.get(requested, 0)) > 1)
        message = ""
        if not requested:
            message = "slug is required"
        elif not valid:
            message = _C.PROBLEM_ID_RULE_MESSAGE
        elif duplicate:
            message = "slug duplicated in this import"
        elif exists:
            message = f"problem already exists: {full_requested}"
        ok = bool(requested) and valid and (not duplicate) and (not exists)
        if not ok:
            has_error = True
        suggested = ""
        if not ok:
            base = requested if valid else _slugify_problem_id(requested)
            if not base:
                base = str(row.get("source_slug") or "").strip()
            suggested = _next_available_problem_slug(owner, base)
        rows.append(
            {
                "seq": seq,
                "index": str(row.get("index") or "").strip().upper(),
                "source_slug": str(row.get("source_slug") or "").strip(),
                "package_name": str(row.get("package_name") or "").strip(),
                "slug_input": requested,
                "slug_full": full_requested,
                "valid": valid,
                "exists": exists,
                "duplicate": duplicate,
                "ok": ok,
                "message": message,
                "suggested": suggested,
            }
        )
    return rows, has_error


def _contest_slug_review_state(raw_slug: str, package_name: str) -> dict[str, object]:
    requested = str(raw_slug or "").strip()
    if not requested:
        base = _import_contest_slug_base_from_package_name(package_name)
        suggested = _next_available_contest_slug(base)
        return {
            "requested": "",
            "valid": True,
            "exists": False,
            "suggested": suggested,
            "message": "",
        }
    try:
        normalized = _normalize_contest_slug_required(requested)
    except ValueError as exc:
        return {
            "requested": requested,
            "valid": False,
            "exists": False,
            "suggested": _next_available_contest_slug(_import_contest_slug_base_from_package_name(package_name)),
            "message": str(exc),
        }
    exists = config.db.fetch_one("SELECT id FROM contests WHERE slug=?", [normalized]) is not None
    suggested = _next_available_contest_slug(normalized) if exists else normalized
    return {
        "requested": normalized,
        "valid": True,
        "exists": bool(exists),
        "suggested": suggested,
        "message": f"contest slug already exists: {normalized}" if exists else "",
    }


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
    package_name = str(draft.get("package_name") or "").strip()
    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    owner = str(gctx.get("user", {}).get("username") or "").strip()
    rows, rows_have_error = _build_problem_slug_review_rows(owner, draft_rows, problem_slug_overrides)
    slug_state = _contest_slug_review_state(contest_slug_input, package_name)
    slug_input_value = str(contest_slug_input or "").strip()
    if not slug_input_value:
        slug_input_value = str(slug_state.get("suggested") or "").strip()
    title_input_value = str(contest_title_input or "").strip()
    if not title_input_value:
        title_input_value = str(draft.get("parsed_title") or "").strip()
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        contest_slug_error = str(slug_state.get("message") or "")
    elif bool(slug_state.get("exists")):
        contest_slug_error = str(slug_state.get("message") or "")
    has_error = bool(top_error or rows_have_error or contest_slug_error)
    return _template_response(
        request,
        "contest_import_review.html",
        {
            "user": gctx["user"],
            "default_problem": gctx["default_problem"],
            "active_main": "contests",
            "draft_id": draft_id,
            "package_name": package_name,
            "parsed_title": str(draft.get("parsed_title") or "").strip(),
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
    session_user = str(_session_user(request) or "").strip()
    if not session_user:
        raise HTTPException(status_code=401, detail="authentication required")
    return session_user

def setup_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register', '/setup'} else '/problems'
        return _redirect_response(target, status_code=303)
    if _has_registered_users():
        return _redirect_response('/login', status_code=303, message='setup already completed')
    return _template_response(request, 'setup.html', {'next_path': next_path, 'password_csrf_token': _issue_password_form_csrf_token('setup-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS), 'config_rows': _setup_config_rows()})

def setup_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), confirm_config: str=Form('0'), next: str=Form('/')):
    _enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    try:
        if _has_registered_users():
            raise ValueError('setup already completed')
        if str(confirm_config or '').strip() not in {'1', 'true', 'on', 'yes'}:
            raise ValueError('please confirm current system configuration paths')
        safe_user = _normalize_username_required(_form_text(username))
        proof_token = _form_text(csrf_token).strip()
        proof_value = _form_text(password_proof).strip().lower()
        verifier_value = _form_text(password_verifier).strip().lower()
        salt_value = _form_text(password_salt)
        iter_value = _form_text(password_iters)
        next_path = _form_text(next)
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
        token = _create_session_for_user(int(user_id))
        _audit(int(user_id), None, 'system.setup', {'super_admin': safe_user, 'config_confirmed': True})
    except ValueError as exc:
        return _redirect_response('/setup', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register', '/setup'}:
        target = '/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def login_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else '/problems'
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
        _login_rate_limit_success(rate_limit_key)
        token = _create_session_for_user(int(row['id']))
    except ValueError as exc:
        return _redirect_response('/login', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register'}:
        target = '/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def register_page(request: Request):
    user = _session_user(request)
    next_path = _safe_next_path(request.query_params.get('next'), '/')
    if user:
        target = next_path if next_path not in {'/', '/login', '/register'} else '/problems'
        return _redirect_response(target, status_code=303)
    return _template_response(request, 'register.html', {'next_path': next_path, 'password_csrf_token': _issue_password_form_csrf_token('register-password'), 'password_salt': secrets.token_hex(16), 'password_iters': int(_C.PASSWORD_HASH_ITERS)})

def register_submit(request: Request, username: str=Form(...), password: str=Form(''), password_confirm: str=Form(''), password_verifier: str=Form(''), password_proof: str=Form(''), csrf_token: str=Form(''), password_salt: str=Form(''), password_iters: str=Form(''), next: str=Form('/')):
    _enforce_same_origin_state_change(request)
    _ = (password, password_confirm)
    try:
        safe_user = _normalize_username_required(_form_text(username))
        proof_token = _form_text(csrf_token).strip()
        proof_value = _form_text(password_proof).strip().lower()
        verifier_value = _form_text(password_verifier).strip().lower()
        salt_value = _form_text(password_salt)
        iter_value = _form_text(password_iters)
        next_path = _form_text(next)
        existing = _lookup_user_auth(safe_user)
        if existing is not None:
            raise ValueError('registration failed; username is unavailable')
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
        token = _create_session_for_user(int(user_id))
    except ValueError as exc:
        return _redirect_response('/register', status_code=303, message=str(exc))
    target = _safe_next_path(next_path, '/problems')
    if target in {'/', '/login', '/register'}:
        target = '/problems'
    response = _redirect_response(target, status_code=303)
    response.set_cookie(_C.AUTH_COOKIE_NAME, token, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.AUTH_COOKIE_MAX_AGE, path='/')
    return response

def logout(request: Request):
    identity = _session_identity(request)
    if identity is not None:
        _revoke_session_token(str(identity['token']))
    _revoke_sudo_session_token(str(request.cookies.get(_C.SUDO_COOKIE_NAME, '') or ''))
    response = _redirect_response('/login', status_code=303, message='logged out')
    response.delete_cookie(_C.AUTH_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    response.delete_cookie(_C.SUDO_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
    return response


def sudo_page(request: Request):
    identity = _session_identity(request)
    if identity is None:
        return _redirect_response('/login', status_code=303)
    next_path = _safe_next_path(request.query_params.get('next'), f"/problems/{identity['username']}/settings")
    if _has_sudo_session(request, user_id=int(identity['user_id']), scope=str(_C.SUDO_SCOPE_DESTRUCTIVE)):
        return _redirect_response(next_path, status_code=303)
    auth_row = _lookup_user_auth(str(identity['username']))
    if auth_row is None:
        return _redirect_response('/login', status_code=303, message='user not found')
    password_salt = str(auth_row['password_salt'] or '').strip().lower()
    try:
        password_iters = int(auth_row['password_iters'] or 0)
    except Exception:
        password_iters = 0
    if not _C.HEX_32_RE.fullmatch(password_salt):
        return _redirect_response('/login', status_code=303, message='password metadata unavailable')
    if password_iters <= 0:
        return _redirect_response('/login', status_code=303, message='password metadata unavailable')
    return _template_response(
        request,
        'sudo.html',
        {
            'username': str(identity['username']),
            'next_path': next_path,
            'password_csrf_token': _issue_password_form_csrf_token('sudo-password'),
            'password_salt': password_salt,
            'password_iters': password_iters,
        },
    )


def sudo_submit(request: Request, password: str = Form(''), password_proof: str = Form(''), csrf_token: str = Form(''), next: str = Form('/')):
    _enforce_same_origin_state_change(request)
    identity = _session_identity(request)
    if identity is None:
        return _redirect_response('/login', status_code=303)
    next_path = _safe_next_path(_form_text(next), f"/problems/{identity['username']}/settings")
    try:
        proof_token = _form_text(csrf_token).strip()
        proof_value = _form_text(password_proof).strip().lower()
        if not _verify_password_form_csrf_token(proof_token, 'sudo-password'):
            raise ValueError('invalid password proof')
        row = _lookup_user_auth(str(identity['username']))
        if row is None:
            raise ValueError('invalid password proof')
        verifier = str(row['password_hash'] or '').strip().lower()
        if not _C.HEX_64_RE.fullmatch(verifier):
            raise ValueError('invalid password proof')
        if not _C.HEX_64_RE.fullmatch(proof_value):
            raise ValueError('invalid password proof')
        expected_proof = _password_proof_from_verifier(proof_token, verifier)
        if not secrets.compare_digest(expected_proof, proof_value):
            raise ValueError('invalid password proof')
        token = _create_sudo_session_for_user(int(identity['user_id']), str(_C.SUDO_SCOPE_DESTRUCTIVE))
    except ValueError as exc:
        return _redirect_response(f'/sudo?next={quote_plus(next_path)}', status_code=303, message=str(exc))
    response = _redirect_response(next_path, status_code=303, message='sudo mode enabled')
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
    user = _session_user(request)
    if not user:
        return _redirect_response('/login', status_code=303)
    return _redirect_response('/problems', status_code=303)

def problems_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    entries = _user_participating_problems(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return _template_response(request, 'root_problems.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'problems'})


def problems_root_import_slug_hint(request: Request, user: str = "", filename: str = "", requested_slug: str = ""):
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    payload = run_export_impl.build_import_slug_hint(str(gctx["user"]["username"]), filename, requested_slug)
    return JSONResponse(payload)


def problems_root_import(request: Request, user: str = "", package_upload: UploadFile | None = File(None), problem_slug: str = Form("")):
    _enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    package_name = ""
    package_content: bytes = b""
    try:
        if package_upload is None:
            raise ValueError("package file is required")
        package_name = str(package_upload.filename or "").strip()
        if not package_name:
            raise ValueError("package filename is required")
        package_content = package_upload.file.read()
        imported = run_export_impl.import_package_as_new_problem(
            actor_user_id=int(gctx["user"]["id"]),
            actor_user=str(gctx["user"]["username"]),
            package_name=package_name,
            package_content=package_content,
            requested_slug=str(problem_slug or "").strip(),
            source_problem="",
        )
        target_problem = str(imported.get("target_problem") or "").strip()
        total_tests = int(imported.get("total_tests") or 0)
        package_format = str(imported.get("package_format") or "package").strip()
        msg = f"{package_format} package imported as {target_problem} ({_count_label(total_tests, 'test')})"
        language_warning = run_export_impl.import_statement_language_warning(imported)
        if language_warning:
            msg = f"{msg}; warning: {language_warning}"
        return _redirect_response(f"/problems/{target_problem}/{gctx['user']['username']}/statement", status_code=303, message=msg)
    except Exception as exc:
        msg = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return _redirect_response("/problems", status_code=303, message=msg)

def contests_root_page(request: Request, user: str = ""):
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    entries = _user_contests_overview(int(gctx['user']['id']), limit=_C.API_PROBLEMS_LIST_LIMIT)
    return _template_response(request, 'root_contests.html', {'user': gctx['user'], 'default_problem': gctx['default_problem'], 'entries': entries, 'entries_limit': _C.API_PROBLEMS_LIST_LIMIT, 'active_main': 'contests'})

def contests_root_create(request: Request, user: str = "", contest_slug: str=Form(...), contest_title: str=Form(...)):
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    msg = 'contest created'
    try:
        slug = _normalize_contest_slug_required(contest_slug)
        title = _normalize_contest_title_required(contest_title)
        actor_user_id = int(gctx["user"]["id"])
        created_at = now_iso()

        def _tx(conn: sqlite3.Connection) -> int:
            exists = conn.execute("SELECT id FROM contests WHERE slug=?", [slug]).fetchone()
            if exists is not None:
                raise ValueError("contest slug already exists")
            conn.execute(
                "INSERT INTO contests(slug,title,owner_user_id,created_at) VALUES(?,?,?,?)",
                [slug, title, actor_user_id, created_at],
            )
            contest_row = conn.execute("SELECT id FROM contests WHERE slug=?", [slug]).fetchone()
            if contest_row is None:
                raise RuntimeError("failed to create contest")
            contest_id = int(contest_row["id"])
            conn.execute(
                "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
                [contest_id, actor_user_id, "owner", created_at],
            )
            return contest_id

        try:
            contest_id = int(config.db.write_transaction(_tx))
        except sqlite3.IntegrityError as exc:
            msg_text = str(exc or "").strip().lower()
            if "contests.slug" in msg_text:
                raise ValueError("contest slug already exists") from exc
            raise
        _audit(int(gctx['user']['id']), None, 'contest.create', {'contest_id': contest_id, 'contest_slug': slug, 'contest_title': title, 'linked_current_problem': False})
        msg = f'contest {slug} created'
    except (ValueError, RuntimeError) as exc:
        msg = str(exc)
    return _redirect_response('/contests', status_code=303, message=msg)


def contests_root_import(
    request: Request,
    user: str = "",
    package_upload: UploadFile | None = File(None),
    contest_slug: str = Form(""),
    contest_title: str = Form(""),
):
    _enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
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
        parsed_title = str(parsed.get("title") or "").strip()
        draft_id = _create_contest_import_draft(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            package_name=package_name,
            package_payload=payload,
            contest_slug_input=_form_text(contest_slug).strip(),
            contest_title_input=_form_text(contest_title).strip(),
            parsed_title=parsed_title,
            problem_rows=draft_rows,
        )
        message = f"contest package parsed ({_count_label(len(draft_rows), 'problem')}); review slugs before import"
        return _redirect_response(
            f"/contests/import/review?draft_id={quote_plus(draft_id)}",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    finally:
        if package_upload is not None:
            package_upload.file.close()
    return _redirect_response("/contests", status_code=303, message=message)


def contests_root_import_review(request: Request, user: str = "", draft_id: str = ""):
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    safe_draft_id = str(draft_id or "").strip()
    try:
        draft, _payload_path = _load_contest_import_draft(actor_user_id, actor_username, safe_draft_id)
    except Exception as exc:
        return _redirect_response("/contests", status_code=303, message=str(exc))
    return _render_contest_import_review_page(
        request,
        gctx,
        draft,
        draft_id=safe_draft_id,
        contest_slug_input=str(draft.get("contest_slug_input") or "").strip(),
        contest_title_input=str(draft.get("contest_title_input") or "").strip(),
        problem_slug_overrides={},
    )


async def contests_root_import_confirm(request: Request, user: str = ""):
    _enforce_same_origin_state_change(request)
    active_user = _active_root_user(request, user)
    gctx = _global_user_ctx(active_user)
    actor_user_id = int(gctx["user"]["id"])
    actor_username = str(gctx["user"]["username"])
    created_contest_slug = ""
    form = await request.form()
    draft_id = str(form.get("draft_id") or "").strip()
    contest_slug_input = str(form.get("contest_slug") or "").strip()
    contest_title_input = str(form.get("contest_title") or "").strip()
    try:
        draft, payload_path = _load_contest_import_draft(actor_user_id, actor_username, draft_id)
    except Exception as exc:
        return _redirect_response("/contests", status_code=303, message=str(exc))

    draft_rows_raw = draft.get("problem_rows")
    draft_rows = [dict(item) for item in draft_rows_raw] if isinstance(draft_rows_raw, list) else []
    problem_slug_overrides: dict[int, str] = {}
    for row in draft_rows:
        seq = int(row.get("seq") or 0)
        if seq <= 0:
            continue
        key = f"problem_slug_{seq}"
        problem_slug_overrides[seq] = str(form.get(key) or "").strip()

    review_rows, rows_have_error = _build_problem_slug_review_rows(actor_username, draft_rows, problem_slug_overrides)
    slug_state = _contest_slug_review_state(contest_slug_input, str(draft.get("package_name") or ""))
    contest_slug_error = ""
    if not bool(slug_state.get("valid")):
        contest_slug_error = str(slug_state.get("message") or "")
    elif bool(slug_state.get("exists")):
        contest_slug_error = str(slug_state.get("message") or "")
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

    package_name = str(draft.get("package_name") or "").strip()
    try:
        payload = payload_path.read_bytes()
        parsed = _POLYGON_CONTEST_IMPORT_SERVICE.parse_package(package_name, payload)
        parsed_rows_raw = parsed.get("problems")
        parsed_rows = [dict(item) for item in parsed_rows_raw] if isinstance(parsed_rows_raw, list) else []
        if len(parsed_rows) != len(review_rows):
            raise ValueError("contest package changed; please re-upload and review again")

        target_contest_slug = _resolve_import_contest_slug(contest_slug_input, package_name)
        parsed_title = str(parsed.get("title") or "").strip()
        target_contest_title = _normalize_contest_title_required(
            _form_text(contest_title_input).strip() or parsed_title or target_contest_slug
        )

        now = now_iso()
        config.db.execute(
            "INSERT INTO contests(slug,title,owner_user_id,created_at) VALUES(?,?,?,?)",
            [target_contest_slug, target_contest_title, actor_user_id, now],
        )
        contest_row = config.db.fetch_one("SELECT id FROM contests WHERE slug=?", [target_contest_slug])
        if contest_row is None:
            raise RuntimeError("failed to create contest")
        contest_id = int(contest_row["id"])
        created_contest_slug = target_contest_slug
        config.db.execute(
            "INSERT OR IGNORE INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
            [contest_id, actor_user_id, "owner", now],
        )

        imported_problem_slugs: list[str] = []
        import_warnings: list[str] = []
        used_indices: set[str] = set()
        for idx, row in enumerate(parsed_rows, start=1):
            row_review = review_rows[idx - 1]
            sub_package_name = str(row.get("package_name") or "").strip() or f"problem-{idx}.zip"
            sub_package_bytes = bytes(row.get("package_bytes") or b"")
            if not sub_package_bytes:
                raise ValueError(f"empty problem package payload for #{idx}")
            requested_problem_slug = str(row_review.get("slug_input") or "").strip().lower()
            imported = run_export_impl.import_package_as_new_problem(
                actor_user_id=actor_user_id,
                actor_user=actor_username,
                package_name=sub_package_name,
                package_content=sub_package_bytes,
                requested_slug=requested_problem_slug,
                source_problem="",
                normalize_test_data_newlines=True,
            )
            imported_problem_slug = str(imported.get("target_problem") or "").strip()
            if not imported_problem_slug:
                raise RuntimeError(f"failed to import problem package #{idx}")
            language_warning = run_export_impl.import_statement_language_warning(imported)
            if language_warning:
                import_warnings.append(f"{imported_problem_slug}: {language_warning}")
            problem_row = config.db.fetch_one("SELECT id FROM problems WHERE slug=?", [imported_problem_slug])
            if problem_row is None:
                raise RuntimeError(f"imported problem missing: {imported_problem_slug}")
            contest_problem_idx = _normalize_import_contest_idx(row.get("index"), idx, used_indices)
            config.db.execute(
                """
                INSERT INTO contest_problems(contest_id,idx,problem_id,added_by_user_id,created_at)
                VALUES(?,?,?,?,?)
                """,
                [contest_id, contest_problem_idx, int(problem_row["id"]), actor_user_id, now_iso()],
            )
            imported_problem_slugs.append(imported_problem_slug)

        _audit(
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
        return _redirect_response(
            f"/contests/{target_contest_slug}/{actor_username}/overview",
            status_code=303,
            message=message,
        )
    except Exception as exc:
        message = str(exc)
    if created_contest_slug:
        return _redirect_response(
            f"/contests/{created_contest_slug}/{actor_username}/overview",
            status_code=303,
            message=message,
        )
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
