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
