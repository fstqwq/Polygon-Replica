#!/usr/bin/env python3
"""Install Agent grants and hashed session credentials in a stopped deployment."""

import argparse
import hashlib
import sqlite3
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


def upgrade(connection: sqlite3.Connection) -> dict[str, int]:
    """Apply and verify the stopped-service authorization replacement."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    required_old = {
        "agent_registration_codes",
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

    connection.execute("BEGIN IMMEDIATE")
    try:
        discarded_registration_codes = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_registration_codes"
            ).fetchone()[0]
        )
        discarded_sessions = int(
            connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
        )
        discarded_tokens = int(
            connection.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0]
        )
        discarded_requests = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_access_requests"
            ).fetchone()[0]
        )
        connection.execute(
            """
            ALTER TABLE agent_sessions
            ADD COLUMN general_scope TEXT NOT NULL DEFAULT 'none'
                CHECK(general_scope IN (
                    'none','readonly','workspace','commit'
                ))
            """
        )
        connection.execute(
            "ALTER TABLE agent_sessions ADD COLUMN credential_sha256 TEXT"
        )
        connection.execute("DROP TABLE agent_access_requests")
        connection.execute("DROP TABLE agent_tokens")
        connection.execute("DELETE FROM agent_sessions")
        connection.execute("DELETE FROM agent_registration_codes")
        connection.execute(
            """
            CREATE UNIQUE INDEX idx_agent_sessions_credential_sha256
            ON agent_sessions(credential_sha256)
            """
        )
        connection.execute(_CREATE_GRANTS)
        connection.execute(_CREATE_REQUESTS)
        connection.execute(
            "ALTER TABLE agent_access_requests_new RENAME TO agent_access_requests"
        )
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
            "discarded_registration_codes": discarded_registration_codes,
            "discarded_sessions": discarded_sessions,
            "discarded_tokens": discarded_tokens,
            "discarded_requests": discarded_requests,
        }
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def backup_database(database: Path, backup: Path) -> Path:
    source = database.absolute()
    target = backup.absolute()
    sidecar = Path(str(target) + ".sha256")
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"SQLite database is unavailable: {source}")
    if target == source:
        raise RuntimeError("backup path must differ from the SQLite database")
    if target.exists() or target.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise RuntimeError(f"backup target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise RuntimeError(f"backup directory must not be a symlink: {target.parent}")
    partial = Path(str(target) + ".partial")
    if partial.exists() or partial.is_symlink():
        raise RuntimeError(f"partial backup already exists: {partial}")
    try:
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(partial) as backup_connection,
        ):
            source_connection.backup(backup_connection)
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise RuntimeError(
                    f"backup SQLite integrity check failed: {integrity!r}"
                )
            foreign_keys = backup_connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_keys:
                raise RuntimeError(
                    f"backup foreign key check failed: {foreign_keys!r}"
                )
        partial.replace(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        sidecar.write_text(f"{digest}  {target.name}\n", encoding="ascii")
        return target
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install Agent grants and hashed session credentials in a stopped "
            "deployment"
        ),
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    database = args.db.absolute()
    backup = backup_database(database, args.backup)
    with sqlite3.connect(database, isolation_level=None) as connection:
        summary = upgrade(connection)
    print(
        f"backup={backup} agent identity grants installed: "
        f"discarded_sessions={summary['discarded_sessions']} "
        f"discarded_tokens={summary['discarded_tokens']} "
        f"discarded_requests={summary['discarded_requests']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
