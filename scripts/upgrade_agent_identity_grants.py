#!/usr/bin/env python3
"""Replace per-problem bearer tokens with identity-authenticated grants."""

import argparse
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


_CREATE_GRANTS = """
CREATE TABLE agent_problem_grants (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('readonly','workspace','commit')),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
)
"""

_CREATE_REQUESTS = """
CREATE TABLE agent_access_requests_new (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    requested_scope TEXT NOT NULL
        CHECK(requested_scope IN ('readonly','workspace','commit')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','denied','expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    grant_id TEXT,
    granted_scope TEXT
        CHECK(granted_scope IS NULL OR granted_scope IN (
            'readonly','workspace','commit'
        )),
    grant_expires_at TEXT,
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(grant_id) REFERENCES agent_problem_grants(id)
)
"""


def _object_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?",
        [name],
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _parse_expiry(raw: object) -> datetime | None:
    text = str(raw or "")
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RuntimeError(f"invalid agent token expiry: {text}") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_grant_id(connection: sqlite3.Connection) -> str:
    while True:
        grant_id = f"ag-{secrets.token_hex(8)}"
        exists = connection.execute(
            "SELECT 1 FROM agent_problem_grants WHERE id=?",
            [grant_id],
        ).fetchone()
        if exists is None:
            return grant_id


def upgrade(
    connection: sqlite3.Connection,
    *,
    migration_time: datetime | None = None,
) -> dict[str, int]:
    """Apply and verify the stopped-service authorization replacement."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    required_old = {
        "agent_sessions",
        "agent_access_requests",
        "agent_tokens",
        "users",
        "problems",
    }
    missing = sorted(
        table for table in required_old if not _object_exists(connection, table)
    )
    if missing:
        raise RuntimeError(f"legacy agent schema is missing: {missing!r}")
    if _object_exists(connection, "agent_problem_grants"):
        raise RuntimeError("agent_problem_grants already exists")
    if "general_scope" in _columns(connection, "agent_sessions"):
        raise RuntimeError("agent_sessions.general_scope already exists")
    expected_token_columns = {
        "id",
        "token_hash",
        "agent_session_id",
        "user_id",
        "problem_id",
        "scope",
        "created_at",
        "expires_at",
        "revoked_at",
    }
    if not expected_token_columns.issubset(_columns(connection, "agent_tokens")):
        raise RuntimeError("legacy agent_tokens shape is incomplete")
    integrity_before = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity_before is None or str(integrity_before[0]) != "ok":
        raise RuntimeError(
            f"SQLite integrity check failed before migration: {integrity_before!r}"
        )
    foreign_keys_before = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys_before:
        raise RuntimeError(
            f"foreign key check failed before migration: {foreign_keys_before!r}"
        )

    effective_time = migration_time or datetime.now(timezone.utc)
    if effective_time.tzinfo is None:
        effective_time = effective_time.replace(tzinfo=timezone.utc)
    else:
        effective_time = effective_time.astimezone(timezone.utc)

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            ALTER TABLE agent_sessions
            ADD COLUMN general_scope TEXT NOT NULL DEFAULT 'none'
                CHECK(general_scope IN (
                    'none','readonly','workspace','commit'
                ))
            """
        )
        connection.execute(_CREATE_GRANTS)
        source_semantics: list[tuple[str, int, str, str, str]] = []
        skipped = 0
        tokens = connection.execute(
            """
            SELECT t.id,t.agent_session_id,t.user_id,t.problem_id,t.scope,
                   t.created_at,t.expires_at,t.revoked_at,
                   s.user_id AS session_user_id,s.revoked_at AS session_revoked_at
            FROM agent_tokens t
            JOIN agent_sessions s ON s.id=t.agent_session_id
            ORDER BY t.id
            """
        ).fetchall()
        for token in tokens:
            if int(token["user_id"]) != int(token["session_user_id"]):
                raise RuntimeError(
                    f"agent token user mismatch: {token['id']}"
                )
            inactive = bool(str(token["revoked_at"] or "")) or bool(
                str(token["session_revoked_at"] or "")
            )
            if inactive:
                skipped += 1
                continue
            scope = str(token["scope"] or "")
            if scope not in {"readonly", "workspace", "commit"}:
                raise RuntimeError(f"invalid agent token scope: {token['id']}")
            expiry = _parse_expiry(token["expires_at"])
            if expiry is not None and expiry <= effective_time:
                skipped += 1
                continue
            semantics = (
                str(token["agent_session_id"]),
                int(token["problem_id"]),
                scope,
                str(token["created_at"]),
                str(token["expires_at"] or ""),
            )
            source_semantics.append(semantics)
            connection.execute(
                """
                INSERT INTO agent_problem_grants(
                    id,agent_session_id,problem_id,scope,created_at,
                    expires_at,revoked_at
                ) VALUES(?,?,?,?,?,?,NULL)
                """,
                [
                    _new_grant_id(connection),
                    semantics[0],
                    semantics[1],
                    semantics[2],
                    semantics[3],
                    semantics[4] or None,
                ],
            )

        target_semantics = [
            (
                str(row["agent_session_id"]),
                int(row["problem_id"]),
                str(row["scope"]),
                str(row["created_at"]),
                str(row["expires_at"] or ""),
            )
            for row in connection.execute(
                """
                SELECT agent_session_id,problem_id,scope,created_at,expires_at
                FROM agent_problem_grants
                ORDER BY id
                """
            ).fetchall()
        ]
        if Counter(source_semantics) != Counter(target_semantics):
            raise RuntimeError("agent token to grant conversion is incomplete")

        discarded_requests = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_access_requests"
            ).fetchone()[0]
        )
        connection.execute(_CREATE_REQUESTS)
        connection.execute("DROP TABLE agent_access_requests")
        connection.execute(
            "ALTER TABLE agent_access_requests_new RENAME TO agent_access_requests"
        )
        connection.execute("DROP TABLE agent_tokens")
        connection.execute(
            """
            CREATE INDEX idx_agent_access_requests_session_status_created
            ON agent_access_requests(agent_session_id,status,created_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_agent_problem_grants_session_problem_active
            ON agent_problem_grants(
                agent_session_id,problem_id,revoked_at,expires_at
            )
            """
        )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"foreign key check failed: {foreign_keys!r}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity!r}")
        connection.execute("COMMIT")
        return {
            "converted_grants": len(source_semantics),
            "skipped_tokens": skipped,
            "discarded_requests": discarded_requests,
        }
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replace agent bearer tokens with identity grants in a stopped "
            "deployment"
        ),
    )
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    database = args.db.absolute()
    if database.is_symlink() or not database.is_file():
        raise RuntimeError(f"SQLite database is unavailable: {database}")
    with sqlite3.connect(database, isolation_level=None) as connection:
        summary = upgrade(connection)
    print(
        "agent identity grants installed: "
        f"converted={summary['converted_grants']} "
        f"skipped={summary['skipped_tokens']} "
        f"discarded_requests={summary['discarded_requests']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
