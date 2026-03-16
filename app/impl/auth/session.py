from __future__ import annotations

from fastapi import Request

from app.impl.runtime.config import config

_C = config.constants


def create_session_for_user(user_id: int) -> str:
    return config.auth_service.create_session_for_user(int(user_id))


def create_sudo_session_for_user(user_id: int, scope: str) -> str:
    return config.auth_service.create_sudo_session_for_user(int(user_id), str(scope or ""))


def revoke_session_token(token: str) -> None:
    config.auth_service.revoke_session_token(str(token or ""))


def revoke_sudo_session_token(token: str) -> None:
    config.auth_service.revoke_sudo_session_token(str(token or ""))


def revoke_sudo_sessions_for_user(user_id: int) -> None:
    config.auth_service.revoke_sudo_sessions_for_user(int(user_id))


def session_identity(request: Request) -> dict | None:
    raw = str(request.cookies.get(_C.AUTH_COOKIE_NAME, "")).strip()
    return config.auth_service.session_identity(raw)


def _sudo_identity(request: Request, scope: str) -> dict | None:
    raw = str(request.cookies.get(_C.SUDO_COOKIE_NAME, "")).strip()
    return config.auth_service.sudo_session_identity(raw, str(scope or ""))


def has_sudo_session(request: Request, *, user_id: int, scope: str) -> bool:
    identity = _sudo_identity(request, scope)
    if identity is None:
        return False
    return int(identity["user_id"]) == int(user_id)


def session_user(request: Request) -> str:
    identity = session_identity(request)
    if identity is None:
        return ""
    return str(identity["username"])
