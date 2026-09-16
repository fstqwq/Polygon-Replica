import sqlite3
from concurrent.futures import ThreadPoolExecutor
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
    def test_contest_schema_has_idx_only_roster(self) -> None:
        with isolated_db_connection(self.db) as connection:
            roster_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(contest_problems)"
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
            conn.execute("PRAGMA foreign_keys=OFF")
        with isolated_db_connection(self.db) as conn:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 1)

    def test_nested_connection_leases_isolate_and_rollback_uncommitted_writes(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE lease_probe(value INTEGER NOT NULL)")
        with isolated_db_connection(self.db) as outer:
            outer.execute("INSERT INTO lease_probe(value) VALUES(7)")
            with isolated_db_connection(self.db) as inner:
                self.assertEqual(inner.execute("SELECT COUNT(*) FROM lease_probe").fetchone()[0], 0)
            self.assertEqual(outer.execute("SELECT value FROM lease_probe").fetchone()[0], 7)
        with isolated_db_connection(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lease_probe").fetchone()[0], 0)

    def test_connection_leases_preserve_concurrent_transaction_updates(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE lease_counter(value INTEGER NOT NULL)")
        isolated_db_execute(self.db, "INSERT INTO lease_counter(value) VALUES(0)")

        def increment() -> None:
            for _index in range(10):
                isolated_db_write_transaction(
                    self.db,
                    lambda connection: connection.execute("UPDATE lease_counter SET value=value+1"),
                )

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(increment) for _index in range(4)]
            for future in futures:
                future.result(timeout=10)
        row = isolated_db_fetch_one(self.db, "SELECT value FROM lease_counter")
        assert row is not None
        self.assertEqual(row["value"], 40)

    def test_connection_drain_allows_database_replacement(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE replacement_probe(value INTEGER NOT NULL)")
        isolated_db_execute(self.db, "INSERT INTO replacement_probe VALUES(1)")
        replacement = self.db.path.with_name("replacement.db")
        connection = sqlite3.connect(replacement)
        try:
            connection.execute("CREATE TABLE replacement_probe(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO replacement_probe VALUES(2)")
            connection.commit()
        finally:
            connection.close()
        self.db.close_connections()
        replacement.replace(self.db.path)
        row = isolated_db_fetch_one(self.db, "SELECT value FROM replacement_probe")
        assert row is not None
        self.assertEqual(row["value"], 2)

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
