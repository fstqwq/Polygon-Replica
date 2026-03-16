from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from app.db import DB
from .common import SmokeBase
from .ui_support import _flash_messages_from_response, _request
from app.impl.preview.preview import preview_page, preview_run
from app.impl.runtime.config import config
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.workspace.context_job import _run_export_create_worker
from app.impl.workspace.context_job_helper import _ensure_implicit_verification
from app.service.verification.store import load_verification_record
from app.service.verification.judge_solve import solve_with_judge_backend


class TestBackendMinimal(SmokeBase):
    def test_db_init_drops_runs_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "migration-probe.db"
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
                        summary_json TEXT,
                        artifact_path TEXT NOT NULL,
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
                        summary_json TEXT,
                        artifact_path TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO verifications(
                        id,problem_id,workspace_id,kind,status,summary_json,artifact_path,created_at,finished_at
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
            probe.init()

            with probe.conn() as conn:
                runs_row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
                ).fetchone()
                self.assertIsNone(runs_row)
                kept = conn.execute(
                    "SELECT kind,summary_json FROM verifications WHERE id=?",
                    ["ver-current"],
                ).fetchone()
                self.assertIsNotNone(kept)
                self.assertEqual(str(kept["kind"] or ""), "verification")
                payload = json.loads(str(kept["summary_json"] or "{}"))
                self.assertEqual(str(payload.get("kind") or ""), "verification")
                self.assertIn("runs", payload)
                self.assertIn("runs_order", payload)

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
        artifact_path = self._artifact_root(verification_id)
        artifact_path.mkdir(parents=True, exist_ok=True)
        config.db.execute(
            """
            INSERT INTO verifications(
                id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),NULL)
            """,
            [
                verification_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "",
                "main",
                "verification",
                "running",
                json.dumps({"status": "running", "runs": {}, "runs_order": []}),
                str(artifact_path),
            ],
        )
        row = load_verification_record(config.db, verification_id)
        self.assertIsInstance(row, dict)
        assert row is not None
        self.assertEqual(str(row.get("status") or ""), "running")

    def test_create_verification_record_preserves_existing_artifact_path_when_updating_summary(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = self.random_id("ver-artifact-path")
        original_artifact_path = self._artifact_root(f"{verification_id}-artifact")
        original_artifact_path.mkdir(parents=True, exist_ok=True)
        from app.service.verification.store import create_verification_record

        create_verification_record(
            config.db,
            config.fs_manager,
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit="",
            source_ref="main",
            kind="verification",
            status="running",
            summary={"status": "running", "runs": {}, "runs_order": []},
            artifact_path=original_artifact_path,
        )

        create_verification_record(
            config.db,
            config.fs_manager,
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit="",
            source_ref="main",
            kind="verification",
            status="running",
            summary={"status": "running", "runs": {}, "runs_order": []},
        )

        row = load_verification_record(config.db, verification_id)
        assert row is not None
        self.assertEqual(Path(str(row.get("artifact_path") or "")).resolve(), original_artifact_path.resolve())

    def test_solve_with_judge_backend_recovers_output_from_judgehost_case_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_root = (root / "artifact").resolve()
            artifact_root.mkdir(parents=True, exist_ok=True)
            ans_dir = (root / "ans").resolve()
            ans_dir.mkdir(parents=True, exist_ok=True)
            expected_work_root = (root / "judgehost-domjudge" / "job-abc").resolve()
            result_root = (expected_work_root / "results" / "7").resolve()
            result_root.mkdir(parents=True, exist_ok=True)
            expected_output = b"42\n"
            (result_root / "program.out").write_bytes(expected_output)

            class _FakeJudgehost:
                STATUS_FAILED = "failed"

                def __init__(self) -> None:
                    self.resolve_calls: list[tuple[str, Path | None]] = []

                def status(self) -> dict[str, int]:
                    return {"hosts_online": 1, "hosts_total": 1, "fetch_batch_size": 1}

                def enqueue_task(self, **_kwargs) -> str:
                    return "jt-recover-output"

                def wait_for_task_result(self, task_id: str, timeout_sec: int | None = None) -> dict[str, object]:
                    return {
                        "task_status": "completed",
                        "run_id": "r-solve-main-case-row",
                        "status": "ok",
                        "artifact_path": str(artifact_root),
                        "summary": {
                            "judgehost": {"task_id": task_id},
                            "tests": [
                                {
                                    "test": "001.in",
                                    "verdict": "OK",
                                    "passes": [{"verdict": "OK"}],
                                }
                            ],
                        },
                    }

                def resolve_artifact_blob(self, token: str, *, work_root: Path | None = None) -> bytes | None:
                    self.resolve_calls.append((token, work_root))
                    if token == "results/7/program.out" and work_root == expected_work_root:
                        return expected_output
                    return None

                def _db_fetch_one(self, sql: str, values: list[object]) -> dict[str, object] | None:
                    if "FROM judgehost_domjudge_cases" in sql:
                        return {
                            "id": 7,
                            "output_run_rel": "results/7/program.out",
                            "work_root": str(expected_work_root),
                        }
                    if "FROM judgehost_domjudge_jobs" in sql:
                        return {"work_root": str(expected_work_root)}
                    return None

            fake_service = _FakeJudgehost()
            fake_self = SimpleNamespace(judgehost_task_service=fake_service, db=SimpleNamespace())

            with patch("app.service.verification.judge_solve.load_verification_summary", return_value={}):
                result = solve_with_judge_backend(
                    fake_self,
                    problem=self.problem,
                    username=self.user,
                    artifact_verification_id="ver-recover-output",
                    accepted_source_rel="solutions/ac.cpp",
                    mode="pass-fail",
                    test_files=[Path("001.in")],
                    ans_dir=ans_dir,
                )

            self.assertEqual(result["001.in"]["rc"], 0)
            self.assertEqual((ans_dir / "001.ans").read_bytes(), expected_output)
            self.assertEqual(fake_service.resolve_calls, [("results/7/program.out", expected_work_root)])

    def test_ensure_implicit_verification_accepts_in_place_build_stages_while_status_running(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        verification_id = self.random_id("ver-implicit-ready")
        artifact_path = self._artifact_root(verification_id)
        artifact_path.mkdir(parents=True, exist_ok=True)
        config.db.execute(
            """
            INSERT INTO verifications(
                id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),NULL)
            """,
            [
                verification_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "",
                "main",
                "verification",
                "running",
                json.dumps(
                    {
                        "status": "running",
                        "stage_results": {
                            "generate_input": {"status": "ok", "verification_source": "verification.generate-input"},
                            "solve_main": {"status": "ok", "verification_source": "verification.solve-main"},
                        },
                    }
                ),
                str(artifact_path),
            ],
        )
        with patch.object(config.verification_service, "run_verification", return_value=verification_id):
            resolved_id, created = _ensure_implicit_verification(
                self.problem,
                self.user,
                ctx=ctx,
                force=True,
                for_verification=True,
                verification_id=verification_id,
            )
        self.assertEqual(resolved_id, verification_id)
        self.assertTrue(created)

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
        config.db.execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,artifact_path,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                json.dumps(
                    {
                        "error": "sample verification failed (ver-sample-123): validator failed",
                        "failed_stage": "sample_sync",
                    }
                ),
                str(artifact_path),
            ],
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
        config.db.execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,artifact_path,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                json.dumps(
                    {
                        "error": "sample verification failed (ver-sample-123): main correct solution RE on 001.in: judge verdict RE",
                        "failed_stage": "sample_sync",
                    }
                ),
                str(artifact_path),
            ],
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

    def test_db_write_transaction_retries_on_locked_error(self) -> None:
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
            config.db.write_transaction(
                lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS __retry_tx_probe(id INTEGER PRIMARY KEY)")
            )
        self.assertTrue(state["failed_once"])

    def test_db_write_transaction_rolls_back_on_exception(self) -> None:
        table_name = "__tx_rollback_probe"
        config.db.execute(f"DROP TABLE IF EXISTS {table_name}")
        config.db.execute(f"CREATE TABLE {table_name}(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

        def _tx(conn):
            conn.execute(f"INSERT INTO {table_name}(id,value) VALUES(?,?)", [1, "x"])
            raise RuntimeError("forced rollback")

        with self.assertRaises(RuntimeError):
            config.db.write_transaction(_tx)
        row = config.db.fetch_one(f"SELECT COUNT(*) AS c FROM {table_name}")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["c"] or 0), 0)

    def test_db_schema_has_verifications_kind_status_index(self) -> None:
        row = config.db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_verifications_kind_status'"
        )
        self.assertIsNotNone(row)
