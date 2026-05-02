from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from app.db import DB, now_iso
from app.runtime_value import RuntimeValues
from app.service.platform.hashing import sha256_hex_text


class AuthUserRow(TypedDict):
    id: int
    username: str
    email: str
    email_normalized: str
    email_verified_at: str
    password_hash: str
    password_salt: str
    password_iters: int


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


class PendingRegistrationRow(TypedDict):
    id: str
    username: str
    email: str
    email_normalized: str
    password_hash: str
    password_salt: str
    password_iters: int
    expires_at: str
    used_at: str


class RateLimitHit(TypedDict):
    allowed: bool
    count: int
    limit: int
    retry_after_sec: int


class AuthStore:
    def __init__(self, db: DB, *, constants: RuntimeValues):
        self.db = db
        self.apply_runtime_values(constants)

    def apply_runtime_values(self, constants: RuntimeValues) -> None:
        self._constants = constants

    @staticmethod
    def _parse_iso_utc(raw: str) -> datetime | None:
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except Exception:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def auth_user_row(self, username: str) -> AuthUserRow | None:
        safe_username = username.strip()
        if not self._constants.USER_IDENT_RE.fullmatch(safe_username):
            return None
        row = self.db.fetch_one(
            """
            SELECT id,username,email,email_normalized,email_verified_at,password_hash,password_salt,password_iters
            FROM users
            WHERE LOWER(username)=LOWER(?)
            ORDER BY id ASC
            LIMIT 1
            """,
            [safe_username],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "email": str(row["email"] or ""),
            "email_normalized": str(row["email_normalized"] or ""),
            "email_verified_at": str(row["email_verified_at"] or ""),
            "password_hash": str(row["password_hash"] or ""),
            "password_salt": str(row["password_salt"] or ""),
            "password_iters": int(row["password_iters"] or 0),
        }

    def registration_conflict(self, username: str, email_normalized: str) -> str:
        row = self.db.fetch_one(
            """
            SELECT username,email_normalized
            FROM users
            WHERE LOWER(username)=LOWER(?) OR (email_normalized<>'' AND email_normalized=?)
            LIMIT 1
            """,
            [username, email_normalized],
        )
        if row is None:
            return ""
        if str(row["username"] or "").strip().lower() == str(username or "").strip().lower():
            return "username"
        return "email"

    def registered_user_count(self) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''",
            [],
        )
        if row is None:
            return 0
        return max(0, int(row["c"] or 0))

    def update_password_verifier(
        self,
        *,
        user_id: int,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
    ) -> None:
        self.db.execute(
            """
            UPDATE users
            SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=?
            WHERE id=?
            """,
            [verifier_hex, salt_hex, int(iterations), now_iso(), int(user_id)],
        )

    def create_user_with_password_verifier(
        self,
        *,
        username: str,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
        email: str = "",
        email_normalized: str = "",
        email_verified_at: str = "",
    ) -> int:
        now_text = now_iso()

        def _tx(conn: sqlite3.Connection) -> int:
            existing_username = conn.execute(
                "SELECT 1 FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1",
                [username],
            ).fetchone()
            if existing_username is not None:
                raise ValueError("user already exists")
            has_registered_user = (
                conn.execute(
                    "SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1"
                ).fetchone()
                is not None
            )
            admin_candidates = [0] if has_registered_user else [1, 0]
            for is_admin in admin_candidates:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO users(
                            username,email,email_normalized,email_verified_at,
                            password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin
                        )
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        [
                            username,
                            email,
                            email_normalized,
                            email_verified_at or None,
                            verifier_hex,
                            salt_hex,
                            int(iterations),
                            now_text,
                            now_text,
                            int(is_admin),
                        ],
                    )
                    return int(cursor.lastrowid)
                except sqlite3.IntegrityError as exc:
                    message = str(exc or "").strip().lower()
                    if "users.username" in message:
                        raise ValueError("user already exists") from exc
                    if "users.email_normalized" in message:
                        raise ValueError("email already exists") from exc
                    if int(is_admin) == 1:
                        continue
                    raise
            raise RuntimeError("failed to create user")

        return int(self.db.write_transaction(_tx))

    def bootstrap_super_admin_with_password_verifier(
        self,
        *,
        username: str,
        verifier_hex: str,
        salt_hex: str,
        iterations: int,
    ) -> int:
        now_text = now_iso()

        def _tx(conn: sqlite3.Connection) -> int:
            has_registered_user = (
                conn.execute(
                    "SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1"
                ).fetchone()
                is not None
            )
            if has_registered_user:
                raise ValueError("setup already completed")
            existing_cursor = conn.execute(
                "SELECT id,username,password_hash FROM users WHERE LOWER(username)=LOWER(?) ORDER BY id ASC LIMIT 1",
                [username],
            )
            existing = existing_cursor.fetchone()
            if existing is None:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO users(
                            username,password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin
                        )
                        VALUES(?,?,?,?,?,?,1)
                        """,
                        [username, verifier_hex, salt_hex, int(iterations), now_text, now_text],
                    )
                except sqlite3.IntegrityError as exc:
                    message = str(exc or "").strip().lower()
                    if "users.username" in message:
                        raise ValueError("setup failed; username is unavailable") from exc
                    raise
                user_id = int(cursor.lastrowid)
            else:
                current_hash = str(existing["password_hash"] or "").strip()
                if current_hash:
                    raise ValueError("setup failed; username is unavailable")
                user_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=?,is_system_admin=1
                    WHERE id=?
                    """,
                    [verifier_hex, salt_hex, int(iterations), now_text, user_id],
                )
            conn.execute("UPDATE users SET is_system_admin=0 WHERE id<>?", [user_id])
            return user_id

        return int(self.db.write_transaction(_tx))

    def hit_rate_limit(self, bucket_key: str, *, limit: int, window_sec: int) -> RateLimitHit:
        safe_bucket = bucket_key.strip()
        safe_limit = max(1, int(limit))
        safe_window = max(1, int(window_sec))
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        window_expires_at = (now + timedelta(seconds=safe_window)).isoformat()

        def _tx(conn: sqlite3.Connection) -> RateLimitHit:
            row = conn.execute(
                "SELECT count,window_expires_at FROM auth_rate_limits WHERE bucket_key=?",
                [safe_bucket],
            ).fetchone()
            if row is None or str(row["window_expires_at"] or "") <= now_text:
                count = 1
                conn.execute(
                    """
                    INSERT INTO auth_rate_limits(bucket_key,count,window_expires_at,updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(bucket_key) DO UPDATE SET
                        count=excluded.count,
                        window_expires_at=excluded.window_expires_at,
                        updated_at=excluded.updated_at
                    """,
                    [safe_bucket, count, window_expires_at, now_text],
                )
                return {
                    "allowed": True,
                    "count": count,
                    "limit": safe_limit,
                    "retry_after_sec": 0,
                }
            count = int(row["count"] or 0) + 1
            expires_at = str(row["window_expires_at"] or now_text)
            conn.execute(
                """
                UPDATE auth_rate_limits
                SET count=?,updated_at=?
                WHERE bucket_key=?
                """,
                [count, now_text, safe_bucket],
            )
            retry_after = self._retry_after_sec(now, expires_at)
            return {
                "allowed": count <= safe_limit,
                "count": count,
                "limit": safe_limit,
                "retry_after_sec": retry_after,
            }

        return self.db.write_transaction(_tx)

    @staticmethod
    def _retry_after_sec(now: datetime, expires_at: str) -> int:
        try:
            parsed = datetime.fromisoformat(expires_at)
        except Exception:
            return 1
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(1, int((parsed.astimezone(timezone.utc) - now).total_seconds()))

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
        pending_id = f"reg-{secrets.token_hex(12)}"
        now_text = now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_sec)))
        ).isoformat()
        self.db.execute(
            """
            INSERT INTO pending_registrations(
                id,username,email,email_normalized,password_hash,password_salt,password_iters,
                token_hash,request_ip,user_agent,terms_accepted,created_at,expires_at,used_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,NULL)
            """,
            [
                pending_id,
                username,
                email,
                email_normalized,
                verifier_hex,
                salt_hex,
                int(iterations),
                token_hash,
                request_ip,
                user_agent,
                now_text,
                expires_at,
            ],
        )
        return pending_id

    def pending_registration_by_token_hash(self, token_hash: str) -> PendingRegistrationRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,username,email,email_normalized,password_hash,password_salt,password_iters,expires_at,used_at
            FROM pending_registrations
            WHERE token_hash=?
            """,
            [token_hash],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "username": str(row["username"] or ""),
            "email": str(row["email"] or ""),
            "email_normalized": str(row["email_normalized"] or ""),
            "password_hash": str(row["password_hash"] or ""),
            "password_salt": str(row["password_salt"] or ""),
            "password_iters": int(row["password_iters"] or 0),
            "expires_at": str(row["expires_at"] or ""),
            "used_at": str(row["used_at"] or ""),
        }

    def activate_pending_registration(self, token_hash: str) -> int:
        now_text = now_iso()

        def _tx(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                """
                SELECT id,username,email,email_normalized,password_hash,password_salt,password_iters,expires_at,used_at
                FROM pending_registrations
                WHERE token_hash=?
                """,
                [token_hash],
            ).fetchone()
            if row is None:
                raise ValueError("registration verification failed")
            if str(row["used_at"] or ""):
                raise ValueError("registration code has already been used")
            if str(row["expires_at"] or "") <= now_text:
                raise ValueError("registration code has expired")
            username = str(row["username"] or "")
            email = str(row["email"] or "")
            email_normalized = str(row["email_normalized"] or "")
            conflict = conn.execute(
                """
                SELECT username,email_normalized
                FROM users
                WHERE LOWER(username)=LOWER(?) OR (email_normalized<>'' AND email_normalized=?)
                LIMIT 1
                """,
                [username, email_normalized],
            ).fetchone()
            if conflict is not None:
                raise ValueError("registration failed; username or email is unavailable")
            is_admin = (
                conn.execute(
                    "SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1"
                ).fetchone()
                is None
            )
            cursor = conn.execute(
                """
                INSERT INTO users(
                    username,email,email_normalized,email_verified_at,
                    password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin
                )
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    username,
                    email,
                    email_normalized,
                    now_text,
                    str(row["password_hash"] or ""),
                    str(row["password_salt"] or ""),
                    int(row["password_iters"] or 0),
                    now_text,
                    now_text,
                    1 if is_admin else 0,
                ],
            )
            conn.execute(
                "UPDATE pending_registrations SET used_at=? WHERE id=?",
                [now_text, str(row["id"] or "")],
            )
            return int(cursor.lastrowid)

        return int(self.db.write_transaction(_tx))

    def create_auth_session(self, user_id: int) -> str:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(self._constants.AUTH_COOKIE_MAX_AGE))
        ).isoformat()
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            token_hash = sha256_hex_text(token)
            session_id = f"s-{secrets.token_hex(12)}"
            try:
                self.db.execute(
                    """
                    INSERT INTO auth_sessions(id,user_id,token_hash,created_at,expires_at,revoked_at)
                    VALUES(?,?,?,?,?,NULL)
                    """,
                    [session_id, int(user_id), token_hash, now_iso(), expires_at],
                )
                return token
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("failed to create auth session")

    def create_sudo_session(self, user_id: int, scope: str) -> str:
        safe_scope = scope.strip().lower()
        if not safe_scope:
            raise ValueError("invalid sudo scope")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(self._constants.SUDO_COOKIE_MAX_AGE))
        ).isoformat()
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            token_hash = sha256_hex_text(token)
            session_id = f"sudo-{secrets.token_hex(12)}"
            try:
                self.db.execute(
                    """
                    INSERT INTO sudo_sessions(id,user_id,scope,token_hash,created_at,expires_at,revoked_at)
                    VALUES(?,?,?,?,?,?,NULL)
                    """,
                    [session_id, int(user_id), safe_scope, token_hash, now_iso(), expires_at],
                )
                return token
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("failed to create sudo session")

    def revoke_auth_session(self, token: str) -> None:
        raw_token = token.strip()
        if not raw_token or not self._constants.SESSION_TOKEN_RE.fullmatch(raw_token):
            return
        self.db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            [now_iso(), sha256_hex_text(raw_token)],
        )

    def revoke_auth_sessions_for_user(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            [now_iso(), int(user_id)],
        )

    def revoke_sudo_session(self, token: str) -> None:
        raw_token = token.strip()
        if not raw_token or not self._constants.SESSION_TOKEN_RE.fullmatch(raw_token):
            return
        self.db.execute(
            "UPDATE sudo_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            [now_iso(), sha256_hex_text(raw_token)],
        )

    def revoke_sudo_sessions_for_user(self, user_id: int) -> None:
        self.db.execute(
            "UPDATE sudo_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            [now_iso(), int(user_id)],
        )

    def session_identity(self, token: str) -> AuthSessionIdentity | None:
        raw_token = token.strip()
        if not raw_token or not self._constants.SESSION_TOKEN_RE.fullmatch(raw_token):
            return None
        row = self.db.fetch_one(
            """
            SELECT s.id AS session_id,s.user_id,s.expires_at,u.username
            FROM auth_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.revoked_at IS NULL
            """,
            [sha256_hex_text(raw_token)],
        )
        if row is None:
            return None
        expires_at = self._parse_iso_utc(str(row["expires_at"] or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            self.revoke_auth_session(raw_token)
            return None
        return {
            "session_id": str(row["session_id"]),
            "user_id": int(row["user_id"]),
            "username": str(row["username"]),
            "token": raw_token,
        }

    def sudo_session_identity(self, token: str, scope: str) -> SudoSessionIdentity | None:
        safe_scope = scope.strip().lower()
        if not safe_scope:
            return None
        raw_token = token.strip()
        if not raw_token or not self._constants.SESSION_TOKEN_RE.fullmatch(raw_token):
            return None
        row = self.db.fetch_one(
            """
            SELECT id,user_id,scope,expires_at
            FROM sudo_sessions
            WHERE token_hash=? AND revoked_at IS NULL
            """,
            [sha256_hex_text(raw_token)],
        )
        if row is None:
            return None
        row_scope = str(row["scope"] or "").strip().lower()
        if row_scope != safe_scope:
            self.revoke_sudo_session(raw_token)
            return None
        expires_at = self._parse_iso_utc(str(row["expires_at"] or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            self.revoke_sudo_session(raw_token)
            return None
        return {
            "sudo_session_id": str(row["id"]),
            "user_id": int(row["user_id"]),
            "scope": row_scope,
            "token": raw_token,
        }
