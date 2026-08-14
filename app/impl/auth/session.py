from fastapi import HTTPException, Request

from app.impl.runtime.dependency import runtime
from app.service.disk.auth_store import AuthSessionIdentity, SudoSessionIdentity



def create_session_for_user(user_id: int) -> str:
    return runtime().auth_service.create_session_for_user(int(user_id))


def create_sudo_session_for_user(user_id: int, scope: str) -> str:
    return runtime().auth_service.create_sudo_session_for_user(int(user_id), str(scope or ""))


def revoke_session_token(token: str) -> None:
    runtime().auth_service.revoke_session_token(str(token or ""))


def revoke_sudo_session_token(token: str) -> None:
    runtime().auth_service.revoke_sudo_session_token(str(token or ""))


def revoke_sudo_sessions_for_user(user_id: int) -> None:
    runtime().auth_service.revoke_sudo_sessions_for_user(int(user_id))


def session_identity(request: Request) -> AuthSessionIdentity | None:
    cookie_name = runtime().config_values.text("AUTH_COOKIE_NAME")
    raw = str(request.cookies.get(cookie_name, "")).strip()
    return runtime().auth_service.session_identity(raw)


def _sudo_identity(request: Request, scope: str) -> SudoSessionIdentity | None:
    cookie_name = runtime().config_values.text("SUDO_COOKIE_NAME")
    raw = str(request.cookies.get(cookie_name, "")).strip()
    return runtime().auth_service.sudo_session_identity(raw, str(scope or ""))


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


def require_session_user(request: Request) -> str:
    username = session_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="login required")
    return username
