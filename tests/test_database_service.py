import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.db import DB, SchemaRequirementsError
from app.service.execution.codec import execution_result_json
from scripts.index_verification_artifacts import upgrade
from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import (
    isolated_db_connection,
    isolated_db_execute,
    isolated_db_fetch_one,
    isolated_db_write_transaction,
)
from tests.verification_service_fixture import make_execution_result, multi_pass_result


class TestDatabaseService(DBTestBase):
    @staticmethod
    def _legacy_artifact_database(path: Path, *, malformed: bool = False) -> None:
        input_ref = "blob://legacy-shared-input"
        answer_ref = "blob://legacy-answer"
        generator_result = (
            "{malformed"
            if malformed
            else execution_result_json(
                make_execution_result(verdict="OK", output_ref=input_ref)
            )
        )
        accepted_result = execution_result_json(multi_pass_result(answer_ref))
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE verifications(id TEXT PRIMARY KEY);
                CREATE TABLE verification_tasks(
                    id TEXT PRIMARY KEY,
                    verification_id TEXT NOT NULL,
                    test_name TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    final_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    FOREIGN KEY(verification_id) REFERENCES verifications(id)
                );
                CREATE TABLE verification_artifact_refs(
                    verification_id TEXT NOT NULL,
                    test_name TEXT NOT NULL,
                    input_ref TEXT NOT NULL,
                    answer_ref TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(verification_id,test_name),
                    FOREIGN KEY(verification_id) REFERENCES verifications(id)
                );
                """
            )
            connection.execute("INSERT INTO verifications(id) VALUES('ver-old')")
            connection.executemany(
                """
                INSERT INTO verification_tasks(
                    id,verification_id,test_name,task_kind,result_json,final_status
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    (
                        "task-generate",
                        "ver-old",
                        "001.in",
                        "generate-input",
                        generator_result,
                        "done",
                    ),
                    (
                        "task-accepted",
                        "ver-old",
                        "001.in",
                        "main-correct",
                        accepted_result,
                        "done",
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO verification_artifact_refs(
                    verification_id,test_name,input_ref,answer_ref,updated_at
                ) VALUES('ver-old','001.in',?,?,?)
                """,
                [input_ref, answer_ref, "2026-08-12T00:00:00+00:00"],
            )
            connection.commit()

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

    def test_verification_artifact_upgrade_backfills_completion_and_pass_owners(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "metadata.db"
            self._legacy_artifact_database(database)
            with sqlite3.connect(database, isolation_level=None) as connection:
                summary = upgrade(connection)
                rows = [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT task_id,pass_number,role,artifact_ref,
                               download_filename
                        FROM verification_task_artifacts
                        ORDER BY task_id,pass_number,role
                        """
                    ).fetchall()
                ]

        self.assertEqual(summary["tasks"], 2)
        self.assertEqual(summary["legacy_completion_refs"], 2)
        self.assertEqual(summary["artifact_rows"], len(rows))
        self.assertIn(
            (
                "task-generate",
                0,
                "generated-input",
                "blob://legacy-shared-input",
                "001.in",
            ),
            rows,
        )
        self.assertIn(
            (
                "task-accepted",
                0,
                "accepted-answer",
                "blob://legacy-answer",
                "001.ans",
            ),
            rows,
        )
        self.assertEqual(
            {
                (int(row[1]), str(row[2]))
                for row in rows
                if str(row[0]) == "task-accepted" and int(row[1]) > 0
            },
            {
                (pass_number, role)
                for pass_number in (1, 2)
                for role in (
                    "pass-compare-metadata",
                    "pass-feedback",
                    "pass-input",
                    "pass-metadata",
                    "pass-output",
                    "pass-stderr",
                    "pass-system",
                    "pass-team-feedback",
                )
            },
        )

    def test_verification_artifact_upgrade_rolls_back_malformed_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "metadata.db"
            self._legacy_artifact_database(database, malformed=True)
            with sqlite3.connect(database, isolation_level=None) as connection:
                with self.assertRaisesRegex(
                    ValueError,
                    "execution result JSON is malformed",
                ):
                    upgrade(connection)
                legacy_row = connection.execute(
                    """
                    SELECT input_ref,answer_ref
                    FROM verification_artifact_refs
                    WHERE verification_id='ver-old' AND test_name='001.in'
                    """
                ).fetchone()

        self.assertEqual(
            None if legacy_row is None else tuple(legacy_row),
            ("blob://legacy-shared-input", "blob://legacy-answer"),
        )
