from typing import TypedDict


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
