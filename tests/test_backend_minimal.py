from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from tests.common import SmokeBase
from app.impl import workspace as workspace_impl
from app.impl.config import config


class TestBackendMinimal(SmokeBase):
    def test_preview_worker_propagates_exception(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        with patch.object(config.preview_service, "compile_preview", side_effect=RuntimeError("preview failed")):
            with self.assertRaises(RuntimeError):
                workspace_impl._run_preview_worker(
                    self.problem,
                    self.user,
                    actor_user_id=int(ctx["user"]["id"]),
                    problem_id=int(ctx["problem"]["id"]),
                    workspace_id=int(ctx["workspace"]["id"]),
                    workspace_head=str(ctx["workspace"].get("head_commit") or ""),
                    workspace_dirty=bool(ctx["workspace"].get("dirty")),
                )

    def test_export_worker_propagates_exception(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        with self.assertRaises(ValueError):
            workspace_impl._run_export_create_worker(
                self.problem,
                self.user,
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                head_commit=str(ctx["workspace"].get("head_commit") or ""),
                requested_build_id="",
                requested_export_type="invalid-type",
            )

    def test_db_conn_enables_foreign_keys(self) -> None:
        with config.db.conn() as conn:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 1)

    def test_db_execute_retries_on_locked_error(self) -> None:
        state = {"failed_once": False}
        original_conn = config.db.conn

        @contextmanager
        def flaky_conn():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise sqlite3.OperationalError("database is locked")
            with original_conn() as conn:
                yield conn

        with patch.object(config.db, "conn", flaky_conn):
            config.db.execute("CREATE TABLE IF NOT EXISTS __retry_probe(id INTEGER PRIMARY KEY)")
        self.assertTrue(state["failed_once"])
