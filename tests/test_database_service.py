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
