import sqlite3
from collections.abc import Callable
from typing import Literal, TypedDict

from app.db import DB, now_iso
from app.service.access.model import AgentGeneralScope, AgentScope
from app.service.access.policy import agent_general_scope, agent_scope


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
    general_scope: AgentGeneralScope
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
    session_revoked_at: str
    problem_id: int
    problem_slug: str
    requested_scope: str
    status: str
    created_at: str
    expires_at: str
    resolved_at: str
    grant_id: str
    granted_scope: str
    grant_expires_at: str


class AgentProblemGrantRow(TypedDict):
    id: str
    agent_session_id: str
    user_id: int
    username: str
    problem_id: int
    problem_slug: str
    scope: AgentScope
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
    general_scope: AgentGeneralScope


class AgentSessionDeleteResult(TypedDict):
    access_request_count: int
    grant_count: int
    session_count: int


class AgentApprovalResult(TypedDict):
    outcome: Literal["approved", "already_approved", "expired"]
    request: AgentAccessRequestRow


ApprovalAccessCheck = Callable[[sqlite3.Connection, int, int, str], bool]


def _session_row(row: sqlite3.Row) -> AgentSessionRow:
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
        "general_scope": agent_general_scope(str(row["general_scope"] or "")),
        "revoked_at": str(row["revoked_at"] or ""),
    }


def _access_request_row(row: sqlite3.Row) -> AgentAccessRequestRow:
    return {
        "id": str(row["id"] or ""),
        "agent_session_id": str(row["agent_session_id"] or ""),
        "user_id": int(row["user_id"]),
        "username": str(row["username"] or ""),
        "identity_hash": str(row["identity_hash"] or ""),
        "agent_name": str(row["agent_name"] or ""),
        "desktop_id": str(row["desktop_id"] or ""),
        "init_ts": str(row["init_ts"] or ""),
        "session_revoked_at": str(row["session_revoked_at"] or ""),
        "problem_id": int(row["problem_id"]),
        "problem_slug": str(row["problem_slug"] or ""),
        "requested_scope": str(row["requested_scope"] or ""),
        "status": str(row["status"] or ""),
        "created_at": str(row["created_at"] or ""),
        "expires_at": str(row["expires_at"] or ""),
        "resolved_at": str(row["resolved_at"] or ""),
        "grant_id": str(row["grant_id"] or ""),
        "granted_scope": str(row["granted_scope"] or ""),
        "grant_expires_at": str(row["grant_expires_at"] or ""),
    }


class AgentStore:
    def __init__(self, db: DB):
        self.db = db

    def create_registration_code(
        self,
        *,
        code: str,
        user_id: int,
        expires_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO agent_registration_codes(
                code,user_id,created_at,expires_at,used_at
            ) VALUES(?,?,?,?,NULL)
            """,
            [code, int(user_id), now_iso(), expires_at],
        )

    def claim_registration_code(
        self,
        code: str,
        *,
        now_text: str,
    ) -> AgentRegistrationRow | None:
        safe_code = str(code or "").strip()
        if not safe_code:
            return None

        def transaction(connection: sqlite3.Connection) -> AgentRegistrationRow | None:
            row = connection.execute(
                """
                SELECT c.code,c.user_id,u.username,c.created_at,
                       c.expires_at,c.used_at
                FROM agent_registration_codes c
                JOIN users u ON u.id=c.user_id
                WHERE c.code=?
                """,
                [safe_code],
            ).fetchone()
            if row is None:
                return None
            result: AgentRegistrationRow = {
                "code": str(row["code"] or ""),
                "user_id": int(row["user_id"]),
                "username": str(row["username"] or ""),
                "created_at": str(row["created_at"] or ""),
                "expires_at": str(row["expires_at"] or ""),
                "used_at": str(row["used_at"] or ""),
            }
            if result["used_at"] or result["expires_at"] <= now_text:
                return result
            connection.execute(
                """
                UPDATE agent_registration_codes
                SET used_at=?
                WHERE code=? AND used_at IS NULL
                """,
                [now_text, safe_code],
            )
            result["used_at"] = now_text
            return result

        return self.db.write_transaction(transaction)

    @staticmethod
    def _session_select() -> str:
        return """
            SELECT s.id,s.user_id,u.username,s.identity_hash,s.agent_name,
                   s.desktop_id,s.init_ts,s.created_at,s.last_seen_at,
                   s.general_scope,s.revoked_at
            FROM agent_sessions s
            JOIN users u ON u.id=s.user_id
        """

    def active_session_by_identity(
        self,
        *,
        user_id: int,
        identity_hash: str,
    ) -> AgentSessionRow | None:
        row = self.db.fetch_one(
            self._session_select()
            + """
            WHERE s.user_id=? AND s.identity_hash=? AND s.revoked_at IS NULL
            ORDER BY s.created_at DESC
            LIMIT 1
            """,
            [int(user_id), identity_hash],
        )
        return None if row is None else _session_row(row)

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
                id,user_id,identity_hash,agent_name,desktop_id,init_ts,
                created_at,last_seen_at,general_scope,revoked_at
            ) VALUES(?,?,?,?,?,?,?,?, 'none',NULL)
            """,
            [
                session_id,
                int(user_id),
                identity_hash,
                agent_name,
                desktop_id,
                init_ts,
                created_at,
                created_at,
            ],
        )

    def touch_session(self, session_id: str, *, last_seen_at: str) -> None:
        self.db.execute(
            """
            UPDATE agent_sessions SET last_seen_at=?
            WHERE id=? AND revoked_at IS NULL
            """,
            [last_seen_at, session_id],
        )

    def session_by_id(self, session_id: str) -> AgentSessionRow | None:
        row = self.db.fetch_one(
            self._session_select() + " WHERE s.id=? LIMIT 1",
            [session_id],
        )
        return None if row is None else _session_row(row)

    def set_general_scope(
        self,
        *,
        session_id: str,
        user_id: int,
        general_scope: str,
    ) -> int:
        def transaction(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                UPDATE agent_sessions SET general_scope=?
                WHERE id=? AND user_id=? AND revoked_at IS NULL
                """,
                [general_scope, session_id, int(user_id)],
            )
            return int(cursor.rowcount or 0)

        return self.db.write_transaction(transaction)

    def create_access_request(
        self,
        *,
        request_id: str,
        agent_session_id: str,
        problem_id: int,
        requested_scope: str,
        expires_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO agent_access_requests(
                id,agent_session_id,problem_id,requested_scope,status,
                created_at,expires_at,resolved_at,grant_id,granted_scope,
                grant_expires_at
            ) VALUES(?,?,?,?,'pending',?,?,NULL,NULL,NULL,NULL)
            """,
            [
                request_id,
                agent_session_id,
                int(problem_id),
                requested_scope,
                now_iso(),
                expires_at,
            ],
        )

    @staticmethod
    def _access_request_select() -> str:
        return """
            SELECT r.id,r.agent_session_id,s.user_id,u.username,s.identity_hash,
                   s.agent_name,s.desktop_id,s.init_ts,
                   COALESCE(s.revoked_at,'') AS session_revoked_at,
                   r.problem_id,p.slug AS problem_slug,r.requested_scope,
                   r.status,r.created_at,r.expires_at,
                   COALESCE(r.resolved_at,'') AS resolved_at,
                   COALESCE(r.grant_id,'') AS grant_id,
                   COALESCE(r.granted_scope,'') AS granted_scope,
                   COALESCE(r.grant_expires_at,'') AS grant_expires_at
            FROM agent_access_requests r
            JOIN agent_sessions s ON s.id=r.agent_session_id
            JOIN users u ON u.id=s.user_id
            JOIN problems p ON p.id=r.problem_id
        """

    @classmethod
    def _access_request_from_connection(
        cls,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> AgentAccessRequestRow | None:
        row = connection.execute(
            cls._access_request_select() + " WHERE r.id=? LIMIT 1",
            [request_id],
        ).fetchone()
        return None if row is None else _access_request_row(row)

    def access_request_by_id(
        self,
        request_id: str,
    ) -> AgentAccessRequestRow | None:
        row = self.db.fetch_one(
            self._access_request_select() + " WHERE r.id=? LIMIT 1",
            [request_id],
        )
        return None if row is None else _access_request_row(row)

    def pending_access_request(
        self,
        *,
        agent_session_id: str,
        problem_id: int,
        requested_scope: str,
        now_text: str,
    ) -> AgentAccessRequestRow | None:
        row = self.db.fetch_one(
            self._access_request_select()
            + """
            WHERE r.agent_session_id=? AND r.problem_id=?
              AND r.requested_scope=? AND r.status='pending'
              AND r.expires_at>?
            ORDER BY r.created_at DESC
            LIMIT 1
            """,
            [agent_session_id, int(problem_id), requested_scope, now_text],
        )
        return None if row is None else _access_request_row(row)

    def resolve_pending_request(
        self,
        *,
        request_id: str,
        status: str,
        resolved_at: str,
    ) -> int:
        def transaction(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                UPDATE agent_access_requests
                SET status=?,resolved_at=?
                WHERE id=? AND status='pending'
                """,
                [status, resolved_at, request_id],
            )
            return int(cursor.rowcount or 0)

        return self.db.write_transaction(transaction)

    def approve_access_request(
        self,
        *,
        actor_user_id: int,
        request_id: str,
        grant_id: str,
        granted_scope: str,
        grant_created_at: str,
        grant_expires_at: str | None,
        access_check: ApprovalAccessCheck,
    ) -> AgentApprovalResult:
        def transaction(connection: sqlite3.Connection) -> AgentApprovalResult:
            row = self._access_request_from_connection(connection, request_id)
            if row is None or row["user_id"] != int(actor_user_id):
                raise LookupError("access request not found")
            if row["session_revoked_at"]:
                raise LookupError("access request not found")
            if row["status"] == "approved":
                return {"outcome": "already_approved", "request": row}
            if row["status"] != "pending":
                raise ValueError("access request is no longer pending")
            if row["expires_at"] <= grant_created_at:
                connection.execute(
                    """
                    UPDATE agent_access_requests
                    SET status='expired',resolved_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [grant_created_at, request_id],
                )
                expired = self._access_request_from_connection(
                    connection,
                    request_id,
                )
                if expired is None:
                    raise RuntimeError("expired access request disappeared")
                return {"outcome": "expired", "request": expired}
            if not access_check(
                connection,
                row["user_id"],
                row["problem_id"],
                granted_scope,
            ):
                raise PermissionError(
                    "current problem access does not allow granted scope"
                )
            connection.execute(
                """
                INSERT INTO agent_problem_grants(
                    id,agent_session_id,problem_id,scope,created_at,
                    expires_at,revoked_at
                ) VALUES(?,?,?,?,?,?,NULL)
                """,
                [
                    grant_id,
                    row["agent_session_id"],
                    row["problem_id"],
                    granted_scope,
                    grant_created_at,
                    grant_expires_at,
                ],
            )
            cursor = connection.execute(
                """
                UPDATE agent_access_requests
                SET status='approved',resolved_at=?,grant_id=?,
                    granted_scope=?,grant_expires_at=?
                WHERE id=? AND status='pending'
                """,
                [
                    grant_created_at,
                    grant_id,
                    granted_scope,
                    grant_expires_at,
                    request_id,
                ],
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError("access request approval lost its update")
            approved = self._access_request_from_connection(
                connection,
                request_id,
            )
            if approved is None:
                raise RuntimeError("approved access request disappeared")
            return {"outcome": "approved", "request": approved}

        return self.db.write_transaction(transaction)

    def list_session_grants(
        self,
        session_id: str,
    ) -> list[AgentProblemGrantRow]:
        rows = self.db.fetch_all(
            """
            SELECT g.id,g.agent_session_id,s.user_id,u.username,g.problem_id,
                   p.slug AS problem_slug,g.scope,g.created_at,g.expires_at,
                   g.revoked_at
            FROM agent_problem_grants g
            JOIN agent_sessions s ON s.id=g.agent_session_id
            JOIN users u ON u.id=s.user_id
            JOIN problems p ON p.id=g.problem_id
            WHERE g.agent_session_id=?
            ORDER BY g.revoked_at IS NOT NULL ASC,g.created_at DESC,g.id
            """,
            [session_id],
        )
        return [
            {
                "id": str(row["id"] or ""),
                "agent_session_id": str(row["agent_session_id"] or ""),
                "user_id": int(row["user_id"]),
                "username": str(row["username"] or ""),
                "problem_id": int(row["problem_id"]),
                "problem_slug": str(row["problem_slug"] or ""),
                "scope": agent_scope(str(row["scope"] or "")),
                "created_at": str(row["created_at"] or ""),
                "expires_at": str(row["expires_at"] or ""),
                "revoked_at": str(row["revoked_at"] or ""),
            }
            for row in rows
        ]

    def revoke_grant(
        self,
        *,
        grant_id: str,
        user_id: int,
        revoked_at: str,
    ) -> int:
        def transaction(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                """
                UPDATE agent_problem_grants
                SET revoked_at=?
                WHERE id=? AND revoked_at IS NULL
                  AND EXISTS(
                      SELECT 1 FROM agent_sessions s
                      WHERE s.id=agent_problem_grants.agent_session_id
                        AND s.user_id=?
                  )
                """,
                [revoked_at, grant_id, int(user_id)],
            )
            return int(cursor.rowcount or 0)

        return self.db.write_transaction(transaction)

    def delete_session_state(
        self,
        *,
        session_id: str,
        user_id: int,
    ) -> AgentSessionDeleteResult:
        def transaction(connection: sqlite3.Connection) -> AgentSessionDeleteResult:
            owned = connection.execute(
                """
                SELECT 1 FROM agent_sessions
                WHERE id=? AND user_id=? LIMIT 1
                """,
                [session_id, int(user_id)],
            ).fetchone()
            if owned is None:
                return {
                    "access_request_count": 0,
                    "grant_count": 0,
                    "session_count": 0,
                }
            access_request_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM agent_access_requests
                    WHERE agent_session_id=?
                    """,
                    [session_id],
                ).fetchone()[0]
            )
            grant_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM agent_problem_grants
                    WHERE agent_session_id=?
                    """,
                    [session_id],
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM agent_access_requests WHERE agent_session_id=?",
                [session_id],
            )
            connection.execute(
                "DELETE FROM agent_problem_grants WHERE agent_session_id=?",
                [session_id],
            )
            session_count = int(
                connection.execute(
                    """
                    DELETE FROM agent_sessions WHERE id=? AND user_id=?
                    """,
                    [session_id, int(user_id)],
                ).rowcount
                or 0
            )
            return {
                "access_request_count": access_request_count,
                "grant_count": grant_count,
                "session_count": session_count,
            }

        return self.db.write_transaction(transaction)

    def list_user_sessions(self, user_id: int) -> list[AgentSessionListRow]:
        rows = self.db.fetch_all(
            """
            SELECT id,user_id,identity_hash,agent_name,desktop_id,init_ts,
                   created_at,last_seen_at,general_scope
            FROM agent_sessions
            WHERE user_id=? AND revoked_at IS NULL
            ORDER BY last_seen_at DESC,created_at DESC
            """,
            [int(user_id)],
        )
        return [
            {
                "id": str(row["id"] or ""),
                "user_id": int(row["user_id"]),
                "identity_hash": str(row["identity_hash"] or ""),
                "agent_name": str(row["agent_name"] or ""),
                "desktop_id": str(row["desktop_id"] or ""),
                "init_ts": str(row["init_ts"] or ""),
                "created_at": str(row["created_at"] or ""),
                "last_seen_at": str(row["last_seen_at"] or ""),
                "general_scope": agent_general_scope(
                    str(row["general_scope"] or "")
                ),
            }
            for row in rows
        ]
