import app.main_constant as _K

from fastapi import Request

from app.impl.auth.session import session_user
from app.impl.auth.shared import (
    _apply_security_headers,
    enforce_same_origin_state_change,
    login_redirect,
)

async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in {"/login", "/register", "/setup"}:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    protected = (
        path == "/"
        or path in {"/problems", "/contests", "/settings"}
        or path == "/agent"
        or (path.startswith("/agent/") and (not path.startswith("/agent/v1/")))
        or path.startswith("/problems/")
        or path.startswith("/contests/")
        or path.startswith("/settings/")
        or path.startswith("/switch-")
        or path.startswith("/sudo")
        or (path == "/logout")
    )
    if not protected:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    user = session_user(request)
    if not user:
        response = login_redirect(request)
        _apply_security_headers(response)
        return response
    enforce_same_origin_state_change(request)
    if _K.ROOT_PROBLEMS_PATH_RE.fullmatch(path) or _K.ROOT_CONTESTS_PATH_RE.fullmatch(path):
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    response = await call_next(request)
    _apply_security_headers(response)
    return response
