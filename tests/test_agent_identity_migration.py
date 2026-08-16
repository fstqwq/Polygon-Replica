import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.upgrade_agent_identity_grants import backup_database, upgrade


_LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL
);
CREATE TABLE problems (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL
);
CREATE TABLE agent_registration_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    identity_hash TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    desktop_id TEXT NOT NULL,
    init_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(user_id,identity_hash),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE agent_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    agent_session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    problem_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id)
);
CREATE TABLE agent_access_requests (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    delivered_at TEXT,
    token_id TEXT,
    delivery_token TEXT,
    FOREIGN KEY(agent_session_id) REFERENCES agent_sessions(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(token_id) REFERENCES agent_tokens(id)
);
CREATE INDEX idx_agent_access_requests_session_status_created
ON agent_access_requests(agent_session_id,status,created_at DESC);
"""


class TestAgentIdentityMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "metadata.db"
        self.connection = sqlite3.connect(self.database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_LEGACY_SCHEMA)
        self.connection.execute("INSERT INTO users(id,username) VALUES(1,'alice')")
        self.connection.execute("INSERT INTO problems(id,slug) VALUES(1,'alice/a')")
        self.connection.execute("INSERT INTO problems(id,slug) VALUES(2,'alice/b')")
        self.connection.execute(
            """
            INSERT INTO agent_registration_codes(
                code,user_id,created_at,expires_at,used_at
            ) VALUES('reg-old',1,'2025-01-01','2027-01-01',NULL)
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_sessions(
                id,user_id,identity_hash,agent_name,desktop_id,init_ts,
                created_at,last_seen_at,revoked_at
            ) VALUES('as-live',1,'identity-live','Codex','desktop','init',
                     '2025-01-01','2025-01-01',NULL)
            """
        )
        self.connection.execute(
            """
            INSERT INTO agent_sessions(
                id,user_id,identity_hash,agent_name,desktop_id,init_ts,
                created_at,last_seen_at,revoked_at
            ) VALUES('as-disconnected',1,'identity-old','Codex','old','init',
                     '2025-01-01','2025-01-01','2025-12-01')
            """
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _token(
        self,
        token_id: str,
        *,
        session_id: str = "as-live",
        problem_id: int = 1,
        scope: str = "readonly",
        created_at: str = "2025-01-01T00:00:00+00:00",
        expires_at: str | None = "2027-01-01T00:00:00+00:00",
        revoked_at: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO agent_tokens(
                id,token_hash,agent_session_id,user_id,problem_id,scope,
                created_at,expires_at,revoked_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                token_id,
                f"hash-{token_id}",
                session_id,
                1,
                problem_id,
                scope,
                created_at,
                expires_at,
                revoked_at,
            ],
        )

    def test_legacy_agent_state_is_discarded(self) -> None:
        self._token("finite-commit", scope="commit")
        self._token(
            "forever-readonly",
            scope="readonly",
            expires_at=None,
            created_at="2025-02-01T00:00:00+00:00",
        )
        self._token(
            "second-finite-readonly",
            scope="readonly",
            expires_at="2028-01-01T00:00:00+00:00",
            created_at="2025-03-01T00:00:00+00:00",
        )
        self._token("expired", expires_at="2025-01-02T00:00:00+00:00")
        self._token("revoked", revoked_at="2025-02-01T00:00:00+00:00")
        self._token("disconnected", session_id="as-disconnected")
        self.connection.execute(
            """
            INSERT INTO agent_access_requests(
                id,agent_session_id,problem_id,status,created_at,expires_at,
                resolved_at,delivered_at,token_id,delivery_token
            ) VALUES('ar-old','as-live',1,'approved','2025-01-01',
                     '2027-01-01','2025-01-01','2025-01-01',
                     'finite-commit','discard-me')
            """
        )

        summary = upgrade(self.connection)

        self.assertEqual(summary["discarded_sessions"], 2)
        self.assertEqual(summary["discarded_tokens"], 6)
        self.assertEqual(summary["discarded_requests"], 1)
        self.assertEqual(summary["discarded_registration_codes"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM agent_problem_grants"
            ).fetchone()[0],
            0,
        )
        request_count = self.connection.execute(
            "SELECT COUNT(*) FROM agent_access_requests"
        ).fetchone()[0]
        self.assertEqual(request_count, 0)
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )

    def test_foreign_key_failure_preserves_legacy_data(self) -> None:
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute(
            """
            INSERT INTO agent_tokens(
                id,token_hash,agent_session_id,user_id,problem_id,scope,
                created_at,expires_at,revoked_at
            ) VALUES('broken','hash-broken','as-missing',1,1,'readonly',
                     '2025-01-01',NULL,NULL)
            """
        )

        with self.assertRaisesRegex(RuntimeError, "foreign key check failed"):
            upgrade(self.connection)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0],
            1,
        )

    def test_backup_is_verified_before_upgrade(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = Path(self.temp_dir.name) / "backup" / "metadata.db"

        created = backup_database(self.database, backup)

        self.assertEqual(created, backup.absolute())
        self.assertTrue(backup.is_file())
        sidecar = Path(str(backup) + ".sha256")
        self.assertRegex(
            sidecar.read_text(encoding="ascii"),
            r"^[0-9a-f]{64}  metadata\.db\n$",
        )
        with sqlite3.connect(backup) as copied:
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copied.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            backup_database(self.database, backup)


if __name__ == "__main__":
    unittest.main()
