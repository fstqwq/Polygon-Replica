import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from app.db import DB, SchemaRequirementsError
from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import (
    isolated_db_connection,
    isolated_db_execute,
    isolated_db_fetch_one,
    isolated_db_write_transaction,
)


class TestDatabaseService(DBTestBase):
    def test_contest_schema_has_idx_only_roster_and_ordinal_build_snapshot(self) -> None:
        with isolated_db_connection(self.db) as connection:
            roster_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contest_problems)"
                ).fetchall()
            }
            build_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contest_build_items)"
                ).fetchall()
            }

        self.assertEqual(
            roster_columns,
            {
                "id",
                "contest_id",
                "idx",
                "problem_id",
                "statement_folder",
                "added_by_user_id",
                "created_at",
            },
        )
        self.assertEqual(
            build_columns,
            {
                "id",
                "job_id",
                "contest_problem_id",
                "ordinal",
                "idx",
                "problem_id",
                "statement_folder",
                "source_commit",
                "revision_number",
                "materialization_id",
                "archive_sha256",
            },
        )

    def test_reinitialization_preserves_existing_and_extension_rows(self) -> None:
        timestamp = "2026-08-12T00:00:00+00:00"
        isolated_db_execute(
            self.db,
            "INSERT INTO problems(id,slug,repo_name,created_at) "
            "VALUES(1,'owner/preserved','preserved.git',?)",
            [timestamp],
        )
        isolated_db_execute(
            self.db,
            "CREATE TABLE operator_extension(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)",
        )
        isolated_db_execute(
            self.db,
            "INSERT INTO operator_extension(id,payload) VALUES(1,'preserved')",
        )

        self.db.init()

        problem = isolated_db_fetch_one(
            self.db,
            "SELECT slug FROM problems WHERE id=1",
        )
        extension = isolated_db_fetch_one(
            self.db,
            "SELECT payload FROM operator_extension WHERE id=1",
        )
        self.assertIsNotNone(problem)
        self.assertEqual(str(problem["slug"]), "owner/preserved")
        self.assertIsNotNone(extension)
        self.assertEqual(str(extension["payload"]), "preserved")

    def test_existing_schema_gap_blocks_runtime_without_repairing_database(self) -> None:
        isolated_db_execute(
            self.db,
            "CREATE TABLE operator_extension(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)",
        )
        isolated_db_execute(
            self.db,
            "INSERT INTO operator_extension(id,payload) VALUES(1,'preserved')",
        )
        isolated_db_execute(self.db, "DROP TABLE system_config")

        reopened = DB(self.db.path, config_values=self.config_values)
        with self.assertRaisesRegex(
            SchemaRequirementsError,
            "missing tables: system_config",
        ):
            reopened.init()

        extension = isolated_db_fetch_one(
            self.db,
            "SELECT payload FROM operator_extension WHERE id=1",
        )
        self.assertIsNotNone(extension)
        self.assertEqual(str(extension["payload"]), "preserved")

    def test_legacy_contest_order_schema_is_blocked_until_offline_upgrade(self) -> None:
        with sqlite3.connect(self.db.path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(
                """
                DROP INDEX idx_contest_build_items_job_ordinal;
                DROP INDEX idx_contest_build_items_materialization;
                DROP INDEX idx_contest_problems_problem;
                DROP TABLE contest_build_items;
                DROP TABLE contest_problems;
                CREATE TABLE contest_problems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contest_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    problem_id INTEGER NOT NULL,
                    statement_folder TEXT NOT NULL DEFAULT '',
                    added_by_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE contest_build_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    contest_problem_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    problem_id INTEGER NOT NULL,
                    statement_folder TEXT NOT NULL DEFAULT '',
                    source_commit TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    materialization_id TEXT NOT NULL,
                    archive_sha256 TEXT NOT NULL
                );
                CREATE INDEX idx_contest_problems_problem
                    ON contest_problems(problem_id,created_at DESC);
                CREATE INDEX idx_contest_build_items_job_position
                    ON contest_build_items(job_id,position);
                CREATE INDEX idx_contest_build_items_materialization
                    ON contest_build_items(materialization_id);
                """
            )

        reopened = DB(self.db.path, config_values=self.config_values)
        with self.assertRaises(SchemaRequirementsError) as raised:
            reopened.init()

        self.assertIn("contest_problems.idx", raised.exception.missing_columns)
        self.assertIn("contest_build_items.idx", raised.exception.missing_columns)
        self.assertIn("contest_build_items.ordinal", raised.exception.missing_columns)
        self.assertIn(
            "idx_contest_build_items_job_ordinal",
            raised.exception.missing_indexes,
        )

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
