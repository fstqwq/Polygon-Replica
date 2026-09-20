import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

from app.db import DB, SchemaRequirementsError
from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import (
    isolated_db_execute,
    isolated_db_fetch_one,
    isolated_db_fetch_all,
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

    def test_writer_lease_restores_foreign_key_enforcement(self) -> None:
        with self.db.writer_connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(sqlite3.IntegrityError) as rejected:
            isolated_db_execute(
                self.db,
                "INSERT INTO workspaces(problem_id,user_id,path,updated_at) VALUES(-1,-1,'orphan','now')",
            )
        self.assertEqual(rejected.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY)
        self.assertEqual(isolated_db_fetch_one(self.db, "SELECT COUNT(*) FROM workspaces")[0], 0)

    def test_readers_keep_committed_snapshot_during_writer_transaction(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE lease_probe(value INTEGER NOT NULL)")
        isolated_db_execute(self.db, "INSERT INTO lease_probe VALUES(1)")
        writing = Event()
        release = Event()

        def update(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE lease_probe SET value=2")
            writing.set()
            if not release.wait(timeout=5):
                raise TimeoutError("reader did not release writer")

        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(self.db.write_transaction, update)
            try:
                self.assertTrue(writing.wait(timeout=5))
                with self.db.conn() as reader:
                    reader.execute("BEGIN")
                    self.assertEqual(reader.execute("SELECT value FROM lease_probe").fetchone()[0], 1)
                    release.set()
                    writer.result(timeout=5)
                    self.assertEqual(reader.execute("SELECT value FROM lease_probe").fetchone()[0], 1)
            finally:
                release.set()
        self.assertEqual(isolated_db_fetch_one(self.db, "SELECT value FROM lease_probe")[0], 2)

    def test_read_connection_rejects_writes(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE lease_probe(value INTEGER NOT NULL)")
        with self.db.conn() as reader:
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("INSERT INTO lease_probe VALUES(7)")
        self.assertEqual(isolated_db_fetch_one(self.db, "SELECT COUNT(*) FROM lease_probe")[0], 0)

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

    def test_external_writer_conflict_returns_error_and_next_write_succeeds(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")
        with closing(sqlite3.connect(self.db.path, timeout=0)) as external:
            external.execute("BEGIN IMMEDIATE")
            try:
                for operation in (
                    lambda: isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(1)"),
                    lambda: isolated_db_write_transaction(self.db,
                        lambda conn: conn.execute("INSERT INTO writer_probe VALUES(2)")
                    ),
                ):
                    with self.assertRaises(sqlite3.OperationalError) as error:
                        operation()
                    self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            finally:
                external.rollback()
        isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(3)")
        self.assertEqual([row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe")], [3])

    def test_failed_transaction_callback_runs_once_and_rolls_back(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")
        side_effect = self.db.path.with_name("transaction-side-effects.log")

        def fail(conn: sqlite3.Connection) -> None:
            with side_effect.open("a", encoding="utf-8") as log:
                log.write("requested\n")
            conn.execute("INSERT INTO writer_probe VALUES(1)")
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            isolated_db_write_transaction(self.db, fail)
        self.assertEqual(side_effect.read_text(encoding="utf-8"), "requested\n")
        isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)")
        self.assertEqual([row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe")], [2])

    def test_nested_writer_rejects_and_rolls_back_outer_transaction(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")

        def nested(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO writer_probe VALUES(1)")
            isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)")

        with self.assertRaisesRegex(RuntimeError, "nested database writer"):
            isolated_db_write_transaction(self.db, nested)
        isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(3)")
        self.assertEqual([row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe")], [3])

    def test_interrupted_writer_rolls_back_before_waiting_writer_commits(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")
        writing = Event()
        second_started = Event()
        release = Event()

        def interrupt(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO writer_probe VALUES(1)")
            writing.set()
            if not release.wait(timeout=5):
                raise TimeoutError("writer was not released")
            raise KeyboardInterrupt("interrupted transaction")

        def second_write() -> None:
            second_started.set()
            isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.db.write_transaction, interrupt)
            try:
                self.assertTrue(writing.wait(timeout=5))
                second = pool.submit(second_write)
                self.assertTrue(second_started.wait(timeout=5))
            finally:
                release.set()
            with self.assertRaises(KeyboardInterrupt):
                first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual([row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe")], [2])

    def test_shutdown_waits_for_commit_and_rejects_new_operations(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")
        writing = Event()
        closing_started = Event()
        release = Event()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO writer_probe VALUES(1)")
            writing.set()
            if not release.wait(timeout=5):
                raise TimeoutError("writer was not released")

        def stop() -> None:
            closing_started.set()
            self.db.close_connections(permanent=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(self.db.write_transaction, write)
            try:
                self.assertTrue(writing.wait(timeout=5))
                shutdown = pool.submit(stop)
                self.assertTrue(closing_started.wait(timeout=5))
            finally:
                release.set()
            writer.result(timeout=5)
            shutdown.result(timeout=5)
        for operation in (
            lambda: isolated_db_fetch_one(self.db, "SELECT 1"),
            lambda: isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)"),
            self.db.init,
        ):
            with self.assertRaisesRegex(RuntimeError, "database is closed"):
                operation()
        with closing(sqlite3.connect(self.db.path, timeout=0)) as reopened:
            self.assertEqual(reopened.execute("SELECT value FROM writer_probe").fetchall(), [(1,)])
            self.assertEqual(reopened.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.db.reopen()
        isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)")
        self.assertEqual(
            [row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe ORDER BY value")],
            [1, 2],
        )

    def test_closed_writer_connection_is_replaced_after_failure(self) -> None:
        isolated_db_execute(self.db, "CREATE TABLE writer_probe(value INTEGER NOT NULL)")

        def broken(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO writer_probe VALUES(1)")
            conn.close()
            raise RuntimeError("connection lost")

        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            isolated_db_write_transaction(self.db, broken)
        isolated_db_execute(self.db, "INSERT INTO writer_probe VALUES(2)")
        self.assertEqual([row[0] for row in isolated_db_fetch_all(self.db, "SELECT value FROM writer_probe")], [2])
