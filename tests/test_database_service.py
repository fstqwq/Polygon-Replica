from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from app.db import CURRENT_SCHEMA_COLUMNS

from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import (
    isolated_db_connection,
    isolated_db_execute,
    isolated_db_fetch_one,
    isolated_db_write_transaction,
)


class TestDatabaseService(DBTestBase):
    _OLD_EXPORTS_DDL = """
        CREATE TABLE exports (
            id TEXT PRIMARY KEY,
            problem_id INTEGER NOT NULL,
            materialization_id TEXT NOT NULL,
            export_type TEXT NOT NULL,
            options_hash TEXT NOT NULL,
            filename TEXT NOT NULL,
            archive_rel_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            source_commit TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(materialization_id,export_type,options_hash),
            FOREIGN KEY(problem_id) REFERENCES problems(id),
            FOREIGN KEY(materialization_id)
                REFERENCES problem_package_materializations(id)
        )
    """

    def _install_options_hash_export_shape(self) -> None:
        timestamp = "2026-08-11T00:00:00+00:00"
        isolated_db_execute(
            self.db,
            """
            INSERT INTO problems(id,slug,repo_name,created_at)
            VALUES(1,'owner/p','p.git',?)
            """,
            [timestamp],
        )
        isolated_db_execute(
            self.db,
            """
            INSERT INTO users(
                id,username,email,email_normalized,created_at
            ) VALUES(1,'owner','owner@example.test','owner@example.test',?)
            """,
            [timestamp],
        )
        isolated_db_execute(
            self.db,
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,
                verification_id,status,created_at,checked_at,unavailable_reason
            ) VALUES(
                'pm-old',1,?,1,?,'materializations/old.zip',?,123,
                'ver-old','available',?,?,'')
            """,
            ["a" * 40, "b" * 64, "c" * 64, timestamp, timestamp],
        )

        def replace_exports(connection: sqlite3.Connection) -> None:
            connection.execute("DROP TABLE exports")
            connection.execute(self._OLD_EXPORTS_DDL)

        self.db.write_schema_reset_transaction(replace_exports)
        isolated_db_execute(
            self.db,
            """
            INSERT INTO exports(
                id,problem_id,materialization_id,export_type,options_hash,
                filename,archive_rel_path,sha256,size_bytes,source_commit,created_at
            ) VALUES('export-old',1,'pm-old','icpc',?,'old.zip',
                     'exports/old.zip',?,456,?,?)
            """,
            ["d" * 64, "e" * 64, "a" * 40, timestamp],
        )
        isolated_db_execute(
            self.db,
            """
            INSERT INTO export_jobs(
                id,problem_id,actor_user_id,export_type,source_commit,status,
                materialization_id,export_id,error,created_at,started_at,finished_at
            ) VALUES(
                'job-old',1,1,'icpc',?,'succeeded','pm-old','export-old','',?,?,?)
            """,
            ["a" * 40, timestamp, timestamp, timestamp],
        )

    def test_current_problem_schema_has_no_name_column(self) -> None:
        self.assertNotIn("name", CURRENT_SCHEMA_COLUMNS["problems"])

    def test_current_workspace_schema_has_revision_summary_columns(self) -> None:
        workspace_columns = set(CURRENT_SCHEMA_COLUMNS["workspaces"])
        self.assertTrue(
            {
                "revision_local",
                "revision_upstream",
                "revision_missing",
                "revision_highlight",
                "revision_upstream_higher",
                "revision_ahead_count",
                "revision_behind_count",
            }.issubset(workspace_columns)
        )
        self.assertNotIn("recent_verification_status", workspace_columns)

    def test_legacy_workspace_status_column_is_tolerated_as_extra(self) -> None:
        isolated_db_execute(
            self.db,
            "ALTER TABLE workspaces ADD COLUMN recent_verification_status TEXT",
        )

        self.db.init()

        with isolated_db_connection(self.db) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(workspaces)")
            }
        self.assertIn("recent_verification_status", columns)

    def test_export_schema_uses_only_materialization_and_type_identity(self) -> None:
        with isolated_db_connection(self.db) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(exports)")
            }
            unique_columns = {
                tuple(
                    str(column[2])
                    for column in connection.execute(
                        f'PRAGMA index_info("{str(index[1])}")'
                    ).fetchall()
                )
                for index in connection.execute("PRAGMA index_list(exports)")
                if bool(index[2])
            }
        self.assertNotIn("options_hash", columns)
        self.assertIn(("materialization_id", "export_type"), unique_columns)

    def test_options_hash_export_shape_is_invalidated_atomically(self) -> None:
        self._install_options_hash_export_shape()

        self.db.init()

        job = isolated_db_fetch_one(
            self.db,
            """
            SELECT status,materialization_id,export_id
            FROM export_jobs WHERE id='job-old'
            """,
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "succeeded")
        self.assertEqual(str(job["materialization_id"]), "pm-old")
        self.assertIsNone(job["export_id"])
        export_count = isolated_db_fetch_one(
            self.db,
            "SELECT COUNT(*) AS count FROM exports",
        )
        self.assertIsNotNone(export_count)
        self.assertEqual(int(export_count["count"]), 0)
        isolated_db_execute(
            self.db,
            """
            INSERT INTO exports(
                id,problem_id,materialization_id,export_type,filename,
                archive_rel_path,sha256,size_bytes,source_commit,created_at
            ) VALUES('export-current',1,'pm-old','icpc','current.zip',
                     'exports/current.zip',?,789,?,?)
            """,
            ["f" * 64, "a" * 40, "2026-08-11T01:00:00+00:00"],
        )

    def test_options_hash_export_shape_upgrade_rolls_back_on_failure(self) -> None:
        self._install_options_hash_export_shape()

        with patch(
            "app.sqlite_shape_upgrade._create_current_table",
            side_effect=RuntimeError("forced export table replacement failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced export"):
                self.db.init()

        with isolated_db_connection(self.db) as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(exports)")
            }
            export = connection.execute(
                "SELECT id FROM exports WHERE id='export-old'"
            ).fetchone()
            job = connection.execute(
                "SELECT export_id FROM export_jobs WHERE id='job-old'"
            ).fetchone()
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertIn("options_hash", columns)
        self.assertIsNotNone(export)
        self.assertEqual(str(job["export_id"]), "export-old")
        self.assertEqual(violations, [])

    def test_current_verification_task_schema_has_only_unified_result(self) -> None:
        columns = set(CURRENT_SCHEMA_COLUMNS["verification_tasks"])
        self.assertIn("result_json", columns)
        self.assertTrue(
            {
                "verdict",
                "runtime_sec",
                "memory_kb",
                "answer_correct",
                "output_ref",
            }.isdisjoint(columns)
        )

    def test_current_sanity_schema_has_per_check_messages(self) -> None:
        self.assertIn("status", CURRENT_SCHEMA_COLUMNS["verification_sanity_checks"])
        self.assertIn("checked_count", CURRENT_SCHEMA_COLUMNS["verification_sanity_checks"])
        self.assertIn("verification_sanity_check_messages", CURRENT_SCHEMA_COLUMNS)

    def test_db_conn_enables_foreign_keys(self) -> None:
        with isolated_db_connection(self.db) as conn:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 1)

    def test_db_execute_retries_on_locked_error(self) -> None:
        state = {"failed_once": False}
        original_conn = type(self.db).conn.__get__(self.db, type(self.db))

        @contextmanager
        def flaky_conn():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise sqlite3.OperationalError("database is locked")
            with original_conn() as conn:
                yield conn

        with patch.object(self.db, "conn", flaky_conn):
            isolated_db_execute(self.db, "CREATE TABLE IF NOT EXISTS __retry_probe(id INTEGER PRIMARY KEY)")
        self.assertTrue(state["failed_once"])

    def test_db_write_transaction_retries_on_locked_error(self) -> None:
        state = {"failed_once": False}
        original_conn = type(self.db).conn.__get__(self.db, type(self.db))

        @contextmanager
        def flaky_conn():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise sqlite3.OperationalError("database is locked")
            with original_conn() as conn:
                yield conn

        with patch.object(self.db, "conn", flaky_conn):
            isolated_db_write_transaction(
                self.db,
                lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS __retry_tx_probe(id INTEGER PRIMARY KEY)")
            )
        self.assertTrue(state["failed_once"])

    def test_db_write_transaction_rolls_back_on_exception(self) -> None:
        table_name = "__tx_rollback_probe"
        isolated_db_execute(self.db, f"DROP TABLE IF EXISTS {table_name}")
        isolated_db_execute(self.db, f"CREATE TABLE {table_name}(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

        def _tx(conn):
            conn.execute(f"INSERT INTO {table_name}(id,value) VALUES(?,?)", [1, "x"])
            raise RuntimeError("forced rollback")

        with self.assertRaises(RuntimeError):
            isolated_db_write_transaction(self.db, _tx)
        row = isolated_db_fetch_one(self.db, f"SELECT COUNT(*) AS c FROM {table_name}")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["c"] or 0), 0)

    def test_db_schema_has_verifications_kind_status_index(self) -> None:
        row = isolated_db_fetch_one(
            self.db,
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_verifications_kind_status'"
        )
        self.assertIsNotNone(row)

    def test_db_schema_has_repo_acl_user_problem_index(self) -> None:
        row = isolated_db_fetch_one(
            self.db,
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_repo_acl_user_problem'"
        )
        self.assertIsNotNone(row)
