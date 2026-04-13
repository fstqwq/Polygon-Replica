from __future__ import annotations

import sqlite3
from typing import TypedDict

from app.db import DB, now_iso


class AgentRegistrationRow(TypedDict):
    code: str
    user_id: int
    username: str
    created_at: str
    expires_at: str
    used_at: str


class AgentSessionRow(TypedDict):
    id: str
    user_id: int
    username: str
    identity_hash: str
    agent_name: str
    desktop_id: str
    init_ts: str
    created_at: str
    last_seen_at: str
    revoked_at: str


class AgentAccessRequestRow(TypedDict):
    id: str
    agent_session_id: str
    user_id: int
    username: str
    identity_hash: str
    agent_name: str
    desktop_id: str
    init_ts: str
    problem_id: int
    problem_slug: str
    status: str
    created_at: str
    expires_at: str
    resolved_at: str
    delivered_at: str
    token_id: str
    delivery_token: str


class AgentTokenRow(TypedDict):
    id: str
    agent_session_id: str
    user_id: int
    username: str
    problem_id: int
    problem_slug: str
    scope: str
    created_at: str
    expires_at: str
    revoked_at: str


class AgentSessionListRow(TypedDict):
    id: str
    user_id: int
    identity_hash: str
    agent_name: str
    desktop_id: str
    init_ts: str
    created_at: str
    last_seen_at: str


class AgentSessionDeleteResult(TypedDict):
    access_request_count: int
    token_count: int
    session_count: int


class AgentSessionTokenListRow(TypedDict):
    id: str
    agent_session_id: str
    problem_id: int
    problem_slug: str
    scope: str
    created_at: str
    expires_at: str
    revoked_at: str


class AgentStore:
    def __init__(self, db: DB):
        self.db = db

    def create_registration_code(self, *, code: str, user_id: int, expires_at: str) -> None:
        self.db.execute(
            """
            INSERT INTO agent_registration_codes(code,user_id,created_at,expires_at,used_at)
            VALUES(?,?,?,?,NULL)
            """,
            [code, int(user_id), now_iso(), expires_at],
        )

    def claim_registration_code(self, code: str, *, now_text: str) -> AgentRegistrationRow | None:
        safe_code = str(code or "").strip()
        if not safe_code:
            return None

        def _tx(conn: sqlite3.Connection) -> AgentRegistrationRow | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT c.code,c.user_id,u.username,c.created_at,c.expires_at,c.used_at
                FROM agent_registration_codes c
                JOIN users u ON u.id=c.user_id
                WHERE c.code=?
                """,
                [safe_code],
            ).fetchone()
            if row is None:
                return None
            used_at = str(row["used_at"] or "")
            if used_at:
                return {
                    "code": str(row["code"] or ""),
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "expires_at": str(row["expires_at"] or ""),
                    "used_at": used_at,
                }
            expires_at = str(row["expires_at"] or "")
            if expires_at and expires_at <= now_text:
                return {
                    "code": str(row["code"] or ""),
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "expires_at": expires_at,
                    "used_at": "",
                }
            conn.execute(
                "UPDATE agent_registration_codes SET used_at=? WHERE code=? AND used_at IS NULL",
                [now_text, safe_code],
            )
            return {
                "code": str(row["code"] or ""),
                "user_id": int(row["user_id"]),
                "username": str(row["username"] or ""),
                "created_at": str(row["created_at"] or ""),
                "expires_at": expires_at,
                "used_at": now_text,
            }

        return self.db.write_transaction(_tx)

    def active_session_by_identity(self, *, user_id: int, identity_hash: str) -> AgentSessionRow | None:
        row = self.db.fetch_one(
            """
            SELECT s.id,s.user_id,u.username,s.identity_hash,s.agent_name,s.desktop_id,s.init_ts,
                   s.created_at,s.last_seen_at,s.revoked_at
            FROM agent_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.user_id=? AND s.identity_hash=? AND s.revoked_at IS NULL
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            [int(user_id), str(identity_hash or "")],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "identity_hash": str(row["identity_hash"] or ""),
            "agent_name": str(row["agent_name"] or ""),
            "desktop_id": str(row["desktop_id"] or ""),
            "init_ts": str(row["init_ts"] or ""),
            "created_at": str(row["created_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "revoked_at": str(row["revoked_at"] or ""),
        }

    def insert_session(
        self,
        *,
        session_id: str,
        user_id: int,
        identity_hash: str,
        agent_name: str,
        desktop_id: str,
        init_ts: str,
        created_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO agent_sessions(
                id,user_id,identity_hash,agent_name,desktop_id,init_ts,created_at,last_seen_at,revoked_at
            )
            VALUES(?,?,?,?,?,?,?,?,NULL)
            """,
            [session_id, int(user_id), identity_hash, agent_name, desktop_id, init_ts, created_at, created_at],
        )

    def touch_session(self, session_id: str, *, last_seen_at: str) -> None:
        self.db.execute(
            "UPDATE agent_sessions SET last_seen_at=? WHERE id=? AND revoked_at IS NULL",
            [last_seen_at, str(session_id or "")],
        )

    def session_by_id(self, session_id: str) -> AgentSessionRow | None:
        row = self.db.fetch_one(
            """
            SELECT s.id,s.user_id,u.username,s.identity_hash,s.agent_name,s.desktop_id,s.init_ts,
                   s.created_at,s.last_seen_at,s.revoked_at
            FROM agent_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.id=?
            LIMIT 1
            """,
            [str(session_id or "")],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "identity_hash": str(row["identity_hash"] or ""),
            "agent_name": str(row["agent_name"] or ""),
            "desktop_id": str(row["desktop_id"] or ""),
            "init_ts": str(row["init_ts"] or ""),
            "created_at": str(row["created_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "revoked_at": str(row["revoked_at"] or ""),
        }

    def create_access_request(
        self,
        *,
        request_id: str,
        agent_session_id: str,
        problem_id: int,
        expires_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO agent_access_requests(
                id,agent_session_id,problem_id,status,created_at,expires_at,resolved_at,delivered_at,token_id,delivery_token
            )
            VALUES(?,?,?,'pending',?,?,NULL,NULL,'','')
            """,
            [request_id, agent_session_id, int(problem_id), now_iso(), expires_at],
        )

    def access_request_by_id(self, request_id: str) -> AgentAccessRequestRow | None:
        row = self.db.fetch_one(
            """
            SELECT r.id,r.agent_session_id,s.user_id,u.username,s.identity_hash,s.agent_name,s.desktop_id,s.init_ts,
                   r.problem_id,p.slug AS problem_slug,r.status,r.created_at,r.expires_at,r.resolved_at,r.delivered_at,
                   COALESCE(r.token_id,'') AS token_id,COALESCE(r.delivery_token,'') AS delivery_token
            FROM agent_access_requests r
            JOIN agent_sessions s ON s.id=r.agent_session_id
            JOIN users u ON u.id=s.user_id
            JOIN problems p ON p.id=r.problem_id
            WHERE r.id=?
            LIMIT 1
            """,
            [str(request_id or "")],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "agent_session_id": str(row["agent_session_id"] or ""),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "identity_hash": str(row["identity_hash"] or ""),
            "agent_name": str(row["agent_name"] or ""),
            "desktop_id": str(row["desktop_id"] or ""),
            "init_ts": str(row["init_ts"] or ""),
            "problem_id": int(row["problem_id"]),
            "problem_slug": str(row["problem_slug"] or ""),
            "status": str(row["status"] or ""),
            "created_at": str(row["created_at"] or ""),
            "expires_at": str(row["expires_at"] or ""),
            "resolved_at": str(row["resolved_at"] or ""),
            "delivered_at": str(row["delivered_at"] or ""),
            "token_id": str(row["token_id"] or ""),
            "delivery_token": str(row["delivery_token"] or ""),
        }

    def resolve_access_request(
        self,
        *,
        request_id: str,
        status: str,
        resolved_at: str,
        token_id: str = "",
        delivery_token: str = "",
    ) -> None:
        self.db.execute(
            """
            UPDATE agent_access_requests
            SET status=?,resolved_at=?,token_id=?,delivery_token=?
            WHERE id=?
            """,
            [status, resolved_at, token_id, delivery_token, str(request_id or "")],
        )

    def mark_request_delivered(self, request_id: str, *, delivered_at: str) -> None:
        self.db.execute(
            """
            UPDATE agent_access_requests
            SET delivered_at=?,delivery_token=''
            WHERE id=?
            """,
            [delivered_at, str(request_id or "")],
        )

    def insert_token(
        self,
        *,
        token_id: str,
        token_hash: str,
        agent_session_id: str,
        user_id: int,
        problem_id: int,
        scope: str,
        created_at: str,
        expires_at: str | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO agent_tokens(
                id,token_hash,agent_session_id,user_id,problem_id,scope,created_at,expires_at,revoked_at
            )
            VALUES(?,?,?,?,?,?,?,?,NULL)
            """,
            [token_id, token_hash, agent_session_id, int(user_id), int(problem_id), scope, created_at, expires_at],
        )

    def token_by_hash(self, token_hash: str) -> AgentTokenRow | None:
        row = self.db.fetch_one(
            """
            SELECT t.id,t.agent_session_id,t.user_id,u.username,t.problem_id,p.slug AS problem_slug,
                   t.scope,t.created_at,t.expires_at,t.revoked_at
            FROM agent_tokens t
            JOIN users u ON u.id=t.user_id
            JOIN problems p ON p.id=t.problem_id
            WHERE t.token_hash=?
            LIMIT 1
            """,
            [str(token_hash or "")],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "agent_session_id": str(row["agent_session_id"] or ""),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "problem_id": int(row["problem_id"]),
            "problem_slug": str(row["problem_slug"] or ""),
            "scope": str(row["scope"] or ""),
            "created_at": str(row["created_at"] or ""),
            "expires_at": str(row["expires_at"] or ""),
            "revoked_at": str(row["revoked_at"] or ""),
        }

    def token_by_id(self, token_id: str) -> AgentTokenRow | None:
        row = self.db.fetch_one(
            """
            SELECT t.id,t.agent_session_id,t.user_id,u.username,t.problem_id,p.slug AS problem_slug,
                   t.scope,t.created_at,t.expires_at,t.revoked_at
            FROM agent_tokens t
            JOIN users u ON u.id=t.user_id
            JOIN problems p ON p.id=t.problem_id
            WHERE t.id=?
            LIMIT 1
            """,
            [str(token_id or "")],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "agent_session_id": str(row["agent_session_id"] or ""),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "problem_id": int(row["problem_id"]),
            "problem_slug": str(row["problem_slug"] or ""),
            "scope": str(row["scope"] or ""),
            "created_at": str(row["created_at"] or ""),
            "expires_at": str(row["expires_at"] or ""),
            "revoked_at": str(row["revoked_at"] or ""),
        }

    def revoke_token(self, *, token_id: str, user_id: int, revoked_at: str) -> int:
        def _tx(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE agent_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                [revoked_at, str(token_id or ""), int(user_id)],
            )
            return int(cursor.rowcount or 0)

        return int(self.db.write_transaction(_tx))

    def delete_session_state(self, *, session_id: str, user_id: int) -> AgentSessionDeleteResult:
        def _tx(conn: sqlite3.Connection) -> AgentSessionDeleteResult:
            owned = conn.execute(
                "SELECT 1 FROM agent_sessions WHERE id=? AND user_id=? LIMIT 1",
                [str(session_id or ""), int(user_id)],
            ).fetchone()
            if owned is None:
                return {"access_request_count": 0, "token_count": 0, "session_count": 0}
            access_request_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_access_requests WHERE agent_session_id=?",
                    [str(session_id or "")],
                ).fetchone()[0]
                or 0
            )
            token_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_tokens WHERE agent_session_id=? AND user_id=?",
                    [str(session_id or ""), int(user_id)],
                ).fetchone()[0]
                or 0
            )
            conn.execute(
                "DELETE FROM agent_access_requests WHERE agent_session_id=?",
                [str(session_id or "")],
            )
            conn.execute(
                "DELETE FROM agent_tokens WHERE agent_session_id=? AND user_id=?",
                [str(session_id or ""), int(user_id)],
            )
            session_count = int(
                conn.execute(
                    "DELETE FROM agent_sessions WHERE id=? AND user_id=?",
                    [str(session_id or ""), int(user_id)],
                ).rowcount
                or 0
            )
            return {
                "access_request_count": access_request_count,
                "token_count": token_count,
                "session_count": session_count,
            }

        return self.db.write_transaction(_tx)

    def list_user_sessions(self, user_id: int) -> list[AgentSessionListRow]:
        rows = self.db.fetch_all(
            """
            SELECT id,user_id,identity_hash,agent_name,desktop_id,init_ts,created_at,last_seen_at
            FROM agent_sessions
            WHERE user_id=?
            ORDER BY last_seen_at DESC, created_at DESC
            """,
            [int(user_id)],
        )
        result: list[AgentSessionListRow] = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"] or ""),
                    "user_id": int(row["user_id"]),
                    "identity_hash": str(row["identity_hash"] or ""),
                    "agent_name": str(row["agent_name"] or ""),
                    "desktop_id": str(row["desktop_id"] or ""),
                    "init_ts": str(row["init_ts"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "last_seen_at": str(row["last_seen_at"] or ""),
                }
            )
        return result

    def list_session_tokens(self, session_id: str) -> list[AgentSessionTokenListRow]:
        rows = self.db.fetch_all(
            """
            SELECT t.id,t.agent_session_id,t.problem_id,p.slug AS problem_slug,t.scope,t.created_at,t.expires_at,t.revoked_at
            FROM agent_tokens t
            JOIN problems p ON p.id=t.problem_id
            WHERE t.agent_session_id=?
            ORDER BY t.revoked_at IS NOT NULL ASC, t.created_at DESC
            """,
            [str(session_id or "")],
        )
        result: list[AgentSessionTokenListRow] = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"] or ""),
                    "agent_session_id": str(row["agent_session_id"] or ""),
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": str(row["problem_slug"] or ""),
                    "scope": str(row["scope"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "expires_at": str(row["expires_at"] or ""),
                    "revoked_at": str(row["revoked_at"] or ""),
                }
            )
        return result
