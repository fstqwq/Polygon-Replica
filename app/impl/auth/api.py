from __future__ import annotations

from fastapi import Request

from app.impl.auth.csrf import issue_password_form_csrf_token
from app.impl.auth.internal.dependency import _C
from app.impl.auth.session import session_user
from app.impl.auth.shared import (
    _apply_security_headers,
    enforce_same_origin_state_change,
    login_redirect,
    redirect_response,
    _startup_cancel_audit_inflight,
)

_ = (issue_password_form_csrf_token, _startup_cancel_audit_inflight)


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/") or path in {"/login", "/register", "/setup"}:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    protected = (
        path == "/"
        or path in {"/problems", "/contests"}
        or path.startswith("/problems/")
        or path.startswith("/contests/")
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
    if _C.ROOT_PROBLEMS_PATH_RE.fullmatch(path) or _C.ROOT_CONTESTS_PATH_RE.fullmatch(path):
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    sm = _C.SETTINGS_USER_PATH_RE.match(path)
    if sm:
        if sm.group("user") != user:
            rest = sm.group("rest") or ""
            target = f"/problems/{user}/settings{rest}"
            if request.url.query:
                target += f"?{request.url.query}"
            return redirect_response(target, status_code=303)
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    pm = _C.PROBLEM_USER_PATH_RE.match(path)
    if pm and pm.group("user") != user:
        rest = pm.group("rest") or ""
        target = f"/problems/{pm.group('problem')}/{user}{rest}"
        if request.url.query:
            target += f"?{request.url.query}"
        return redirect_response(target, status_code=303)
    cm = _C.CONTEST_USER_PATH_RE.match(path)
    if cm and cm.group("user") != user:
        rest = cm.group("rest") or ""
        target = f"/contests/{cm.group('contest')}/{user}{rest}"
        if request.url.query:
            target += f"?{request.url.query}"
        return redirect_response(target, status_code=303)
    response = await call_next(request)
    _apply_security_headers(response)
    return response


