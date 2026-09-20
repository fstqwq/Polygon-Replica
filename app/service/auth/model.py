from typing import TypedDict


class AuthUserRow(TypedDict):
    id: int
    username: str
    email: str
    email_normalized: str
    email_verified_at: str
    password_hash: str
    password_salt: str
    password_iters: int
    is_system_admin: int
    is_banned: int
    banned_at: str


class RateLimitHit(TypedDict):
    allowed: bool
    count: int
    limit: int
    retry_after_sec: int


class AuthSessionIdentity(TypedDict):
    session_id: str
    user_id: int
    username: str
    token: str


class SudoSessionIdentity(TypedDict):
    sudo_session_id: str
    user_id: int
    scope: str
    token: str
