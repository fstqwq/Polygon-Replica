from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.db import DB, IncompatibleSchemaError
from .db_helpers import db_connection, db_execute, db_fetch_one, db_write_transaction, write_preview_summary
from .common import SmokeBase
from .ui_support import _flash_messages_from_response, _request
from app.impl.preview.preview import preview_page, preview_run
from app.impl.runtime.config import config
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.workspace.context_job import _run_export_create_worker
from app.service.disk.verification_store import VerificationStore


class TestBackendMinimal(SmokeBase):
    def test_db_init_fails_fast_on_incompatible_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy-schema.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE problems (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        slug TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        repo_name TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT,
                        password_salt TEXT,
                        password_iters INTEGER,
                        password_updated_at TEXT,
                        is_system_admin INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE workspaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        problem_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        branch TEXT,
                        head_commit TEXT,
                        dirty INTEGER NOT NULL DEFAULT 0,
                        recent_verification_status TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(problem_id, user_id),
                        FOREIGN KEY(problem_id) REFERENCES problems(id),
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO problems(id,slug,name,repo_name,created_at) VALUES(1,'p','P','p','2026-03-13T00:00:00Z')"
                )
                conn.execute(
                    "INSERT INTO users(id,username,created_at) VALUES(1,'u','2026-03-13T00:00:00Z')"
                )
                conn.execute(
                    """
                    INSERT INTO workspaces(id,problem_id,user_id,path,branch,head_commit,dirty,recent_verification_status,updated_at)
                    VALUES(1,1,1,'/tmp/ws','','',0,NULL,'2026-03-13T00:00:00Z')
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        problem_id INTEGER NOT NULL,
                        workspace_id INTEGER,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        legacy_payload TEXT,
                        legacy_root TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE verifications (
                        id TEXT PRIMARY KEY,
                        problem_id INTEGER NOT NULL,
                        workspace_id INTEGER,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        legacy_payload TEXT,
                        legacy_root TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO verifications(
                        id,problem_id,workspace_id,kind,status,legacy_payload,legacy_root,created_at,finished_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        "ver-current",
                        1,
                        1,
                        "verification",
                        "running",
                        json.dumps(
                            {
                                "kind": "verification",
                                "status": "running",
                                "runs_order": ["r-one"],
                                "runs": {
                                    "r-one": {
                                        "run_id": "r-one",
                                        "status": "running",
                                        "source_label": "solutions/ac.cpp",
                                        "summary": {"source": "solutions/ac.cpp", "tests": []},
                                    }
                                },
                            }
                        ),
                        str(Path(tmpdir) / "ver-current"),
                        "2026-03-13T00:00:00Z",
                        None,
                    ],
                )
                conn.commit()

            probe = DB(db_path)
            with self.assertRaises(IncompatibleSchemaError):
                probe.init()
            backup_candidates = sorted(db_path.parent.glob(f"{db_path.name}.*.backup"))
            self.assertEqual(backup_candidates, [])

    def test_judgehost_compile_check_reads_full_diagnostics_from_transient_task_result(self) -> None:
        with (
            patch.object(config.judgehost_task_service, "enabled", return_value=True),
            patch.object(config.judgehost_task_service, "auth_token_configured", return_value=True),
            patch.object(config.judgehost_task_service, "status", return_value={"hosts_online": 1}),
            patch.object(
                config.judgehost_task_service,
                "compile_only_submission",
                return_value={
                    "status": "failed",
                    "error": "Compiling failed with exitcode 1, compiler output:",
                    "summary": {
                        "error": "Compiling failed with exitcode 1, compiler output:",
                        "compile_diagnostics": [
                            {
                                "message": "Compiling failed with exitcode 1, compiler output:\nvalidator.cpp:4:35: error: expected ';' before 'inf'"
                            }
                        ],
                    },
                },
            ),
            patch("app.impl.problem.compile_check.workspace_testlib_header", return_value=None),
        ):
            msg = judgehost_compile_check_error(
                problem=self.problem,
                user=self.user,
                workspace=Path("."),
                source_path="validators/validator.cpp",
                source_content="int main(){\n",
                verification_source="problem.validator.save_source",
            )
        self.assertIn("validator.cpp:4:35: error: expected ';' before 'inf'", msg)

    def test_load_verification_record_returns_plain_dict(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = self.random_id("ver-record-dict")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
        )
        row = VerificationStore(config.db).record_row(verification_id)
        self.assertIsInstance(row, dict)
        assert row is not None
        self.assertEqual(str(row.get("status") or ""), "running")

    def test_create_verification_record_uses_canonical_verification_root(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = self.random_id("ver-artifact-path")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
        )

        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
        )

        row = VerificationStore(config.db).record_row(verification_id)
        assert row is not None
        self.assertEqual(
            config.verification_service.artifact_path_for_verification(verification_id),
            str(config.fs_manager.prepare_verification_root(verification_id).resolve()),
        )
        self.assertEqual(
            config.fs_manager.prepare_verification_root(verification_id).resolve(),
            Path(config.verification_service.artifact_path_for_verification(verification_id)).resolve(),
        )

    def test_judgehost_compile_check_surfaces_backend_failure_when_result_is_missing(self) -> None:
        with (
            patch.object(config.judgehost_task_service, "enabled", return_value=True),
            patch.object(config.judgehost_task_service, "auth_token_configured", return_value=True),
            patch.object(config.judgehost_task_service, "status", return_value={"hosts_online": 1}),
            patch.object(
                config.judgehost_task_service,
                "compile_only_submission",
                side_effect=RuntimeError("Compiling failed with exitcode 1, compiler output:"),
            ),
            patch("app.impl.problem.compile_check.workspace_testlib_header", return_value=None),
        ):
            msg = judgehost_compile_check_error(
                problem=self.problem,
                user=self.user,
                workspace=Path("."),
                source_path="validators/validator.cpp",
                source_content="int main(){\n",
                verification_source="problem.validator.save_source",
            )
        self.assertIn("Compiling failed with exitcode 1, compiler output:", msg)

    def test_preview_run_uses_sample_build_failed_flash_for_sample_sync_failure(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        preview_id = self.random_id("p-preview-sample-sync-failed")
        artifact_path = self._artifact_root(preview_id)
        artifact_path.mkdir(parents=True, exist_ok=True)
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,artifact_path,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                str(artifact_path),
            ],
        )
        write_preview_summary(
            preview_id,
            {
                "error": "sample verification failed (ver-sample-123): validator failed",
                "failed_stage": "sample_sync",
            },
        )
        with patch.object(config.preview_service, "compile_preview", return_value=preview_id):
            resp = preview_run(self.problem, self.user, page="statement")
        self.assertEqual(resp.status_code, 303)
        self.assertIn(
            f"/problems/{self.problem}/{self.user}/statement?preview_id={preview_id}",
            resp.headers.get("location", ""),
        )
        self.assertIn("sample verification failed.", _flash_messages_from_response(resp))

    def test_preview_page_shows_full_sample_build_failure_detail(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        preview_id = self.random_id("p-preview-sample-sync-detail")
        artifact_path = self._artifact_root(preview_id)
        artifact_path.mkdir(parents=True, exist_ok=True)
        (artifact_path / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_path / "logs" / "latex.log").write_text(
            "sample verification failed (ver-old): validator failed on tests/spec.json entry 1\n",
            encoding="utf-8",
        )
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,artifact_path,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                str(artifact_path),
            ],
        )
        write_preview_summary(
            preview_id,
            {
                "error": "sample verification failed (ver-sample-123): main correct solution RE on 001.in: judge verdict RE",
                "failed_stage": "sample_sync",
            },
        )
        resp = preview_page(
            _request(
                f"/problems/{self.problem}/{self.user}/statement",
                f"preview_id={preview_id}",
            ),
            self.problem,
            self.user,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Sample verification failed.", html)
        self.assertIn("sample verification failed (ver-sample-123): main correct solution RE on 001.in: judge verdict RE", html)
        self.assertNotIn("Open full latex.log", html)
        self.assertNotIn("sample verification failed (ver-old): validator failed on tests/spec.json entry 1", html)

    def test_preview_worker_propagates_exception(self) -> None:
        with patch.object(config.preview_service, "compile_preview", side_effect=RuntimeError("preview failed")):
            resp = preview_run(self.problem, self.user, page="statement")
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{self.problem}/{self.user}/statement", resp.headers.get("location", ""))

    def test_export_worker_propagates_exception(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        with self.assertRaises(ValueError):
            _run_export_create_worker(
                self.problem,
                self.user,
                actor_user_id=int(ctx["user"]["id"]),
                problem_id=int(ctx["problem"]["id"]),
                workspace_id=int(ctx["workspace"]["id"]),
                head_commit=str(ctx["workspace"].get("head_commit") or ""),
                requested_verification_id="",
                requested_export_type="invalid-type",
            )

    def test_db_conn_enables_foreign_keys(self) -> None:
        with db_connection() as conn:
            row = conn.execute("PRAGMA foreign_keys").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 1)

    def test_db_execute_retries_on_locked_error(self) -> None:
        state = {"failed_once": False}
        original_conn = type(config.db).conn.__get__(config.db, type(config.db))

        @contextmanager
        def flaky_conn():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise sqlite3.OperationalError("database is locked")
            with original_conn() as conn:
                yield conn

        with patch.object(config.db, "conn", flaky_conn):
            db_execute("CREATE TABLE IF NOT EXISTS __retry_probe(id INTEGER PRIMARY KEY)")
        self.assertTrue(state["failed_once"])

    def test_db_write_transaction_retries_on_locked_error(self) -> None:
        state = {"failed_once": False}
        original_conn = type(config.db).conn.__get__(config.db, type(config.db))

        @contextmanager
        def flaky_conn():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise sqlite3.OperationalError("database is locked")
            with original_conn() as conn:
                yield conn

        with patch.object(config.db, "conn", flaky_conn):
            db_write_transaction(
                lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS __retry_tx_probe(id INTEGER PRIMARY KEY)")
            )
        self.assertTrue(state["failed_once"])

    def test_db_write_transaction_rolls_back_on_exception(self) -> None:
        table_name = "__tx_rollback_probe"
        db_execute(f"DROP TABLE IF EXISTS {table_name}")
        db_execute(f"CREATE TABLE {table_name}(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

        def _tx(conn):
            conn.execute(f"INSERT INTO {table_name}(id,value) VALUES(?,?)", [1, "x"])
            raise RuntimeError("forced rollback")

        with self.assertRaises(RuntimeError):
            db_write_transaction(_tx)
        row = db_fetch_one(f"SELECT COUNT(*) AS c FROM {table_name}")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["c"] or 0), 0)

    def test_db_schema_has_verifications_kind_status_index(self) -> None:
        row = db_fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_verifications_kind_status'"
        )
        self.assertIsNotNone(row)
