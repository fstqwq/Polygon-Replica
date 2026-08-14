from app.config import ConfigValues
from app.service.auth.model import AuthSessionIdentity, SudoSessionIdentity
from app.service.disk.auth_store import (
    AuthStore,
    AuthUserRow,
)
from app.service.auth.password_hash import password_verifier_storage_hash


class AuthService:
    def __init__(self, store: AuthStore, *, config_values: ConfigValues):
        self._store = store
        if config_values is not store.config_values:
            raise ValueError("auth service and store must share ConfigValues")

    def lookup_user_auth(self, username: str) -> AuthUserRow | None:
        return self._store.auth_user_row(username)

    def has_registered_users(self) -> bool:
        return self._store.registered_user_count() > 0

    def set_user_password_verifier(self, user_id: int, verifier_hex: str, salt_hex: str, iterations: int) -> None:
        self._store.update_password_verifier(
            user_id=int(user_id),
            verifier_hex=password_verifier_storage_hash(verifier_hex),
            salt_hex=salt_hex,
            iterations=int(iterations),
        )

    def active_system_admin_count(self) -> int:
        return self._store.active_system_admin_count()

    def admin_user_rows(self, *, query: str, limit: int = 50) -> list[dict[str, object]]:
        return [dict(row) for row in self._store.admin_user_rows(query=str(query or ""), limit=int(limit))]

    def revoke_all_access_for_user(self, user_id: int) -> None:
        self._store.revoke_all_access_for_user(int(user_id))

    def set_system_admin(self, *, actor_user_id: int, username: str, enabled: bool) -> dict[str, object]:
        return dict(
            self._store.set_system_admin(
                actor_user_id=int(actor_user_id),
                username=str(username or ""),
                enabled=bool(enabled),
            )
        )

    def set_user_banned(self, *, actor_user_id: int, username: str, banned: bool) -> dict[str, object]:
        return dict(
            self._store.set_user_banned(
                actor_user_id=int(actor_user_id),
                username=str(username or ""),
                banned=bool(banned),
            )
        )

    def create_user_with_password_verifier(
        self,
        username: str,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
        email: str = "",
        email_normalized: str = "",
        email_verified_at: str = "",
    ) -> int:
        return self._store.create_user_with_password_verifier(
            username=username,
            verifier_hex=password_verifier_storage_hash(verifier_hex),
            salt_hex=salt_hex,
            iterations=int(iterations),
            email=email,
            email_normalized=email_normalized,
            email_verified_at=email_verified_at,
        )

    def registration_conflict(self, username: str, email_normalized: str) -> str:
        return self._store.registration_conflict(username, email_normalized)

    def hit_rate_limit(self, bucket_key: str, *, limit: int, window_sec: int) -> dict[str, object]:
        return dict(self._store.hit_rate_limit(bucket_key, limit=int(limit), window_sec=int(window_sec)))

    def create_pending_registration(
        self,
        *,
        username: str,
        email: str,
        email_normalized: str,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
        token_hash: str,
        request_ip: str,
        user_agent: str,
        ttl_sec: int,
    ) -> str:
        return self._store.create_pending_registration(
            username=username,
            email=email,
            email_normalized=email_normalized,
            verifier_hex=password_verifier_storage_hash(verifier_hex),
            salt_hex=salt_hex,
            iterations=int(iterations),
            token_hash=token_hash,
            request_ip=request_ip,
            user_agent=user_agent,
            ttl_sec=int(ttl_sec),
        )

    def pending_registration_by_token_hash(self, token_hash: str) -> dict[str, object] | None:
        row = self._store.pending_registration_by_token_hash(token_hash)
        if row is None:
            return None
        return dict(row)

    def activate_pending_registration(self, token_hash: str) -> int:
        return self._store.activate_pending_registration(token_hash)

    def bootstrap_super_admin_with_password_verifier(
        self,
        username: str,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
    ) -> int:
        return self._store.bootstrap_super_admin_with_password_verifier(
            username=username,
            verifier_hex=password_verifier_storage_hash(verifier_hex),
            salt_hex=salt_hex,
            iterations=int(iterations),
        )

    def create_session_for_user(self, user_id: int) -> str:
        return self._store.create_auth_session(int(user_id))

    def create_sudo_session_for_user(self, user_id: int, scope: str) -> str:
        return self._store.create_sudo_session(int(user_id), scope)

    def revoke_session_token(self, token: str) -> None:
        self._store.revoke_auth_session(token)

    def revoke_auth_sessions_for_user(self, user_id: int) -> None:
        self._store.revoke_auth_sessions_for_user(int(user_id))

    def revoke_sudo_session_token(self, token: str) -> None:
        self._store.revoke_sudo_session(token)

    def revoke_sudo_sessions_for_user(self, user_id: int) -> None:
        self._store.revoke_sudo_sessions_for_user(int(user_id))

    def session_identity(self, token: str) -> AuthSessionIdentity | None:
        return self._store.session_identity(token)

    def sudo_session_identity(
        self,
        token: str,
        scope: str,
    ) -> SudoSessionIdentity | None:
        return self._store.sudo_session_identity(token, scope)
