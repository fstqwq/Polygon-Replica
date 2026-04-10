from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi import HTTPException

from app.db import DB, IncompatibleSchemaError
from .db_helpers import db_connection, db_execute, db_fetch_one, db_write_transaction, write_preview_summary
from .common import SmokeBase
from .ui_support import _flash_messages_from_response, _request
from app.impl.preview.preview import preview_page, preview_run, preview_status, statement_language_add
from app.impl.run_export.artifact import artifact_file
from app.impl.auth.internal.runtime import _startup_clear_all_caches
from app.impl.runtime.config import config
from app.impl.problem.compile_check import judgehost_compile_check_error
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_job import _run_export_create_worker
from app.service.statement.signature import statement_sources_signature
from app.service.disk.verification_store import VerificationStore


class TestBackendMinimal(SmokeBase):
    def test_startup_clear_all_caches_wipes_cache_root_artifacts_and_runtime(self) -> None:
        artifact_file = config.fs_manager.cache_artifacts_root / "verifications" / "ver-test" / "logs" / "compile.log"
        runtime_file = config.fs_manager.runtime_root / "judgehost-runs" / "jt-test" / "stdout.txt"
        durable_log = config.fs_manager.runtime_root / "worker-queue-events.jsonl"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        durable_log.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_text("{}", encoding="utf-8")
        runtime_file.write_text("ok\n", encoding="utf-8")
        durable_log.write_text("event\n", encoding="utf-8")

        with (
            patch.object(config.async_task_cache_service, "clear_all", return_value=None),
            patch.object(config.judge_fs_index_service, "clear_all", return_value=None),
            patch.object(config.judgehost_task_service, "clear_testcase_registry", return_value=None),
        ):
            _startup_clear_all_caches()

        self.assertTrue(config.fs_manager.cache_artifacts_root.exists())
        self.assertTrue(config.fs_manager.runtime_root.exists())
        self.assertFalse(artifact_file.exists())
        self.assertFalse(runtime_file.exists())
        self.assertFalse(durable_log.exists())

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
                        payload_json TEXT,
                        artifact_root TEXT NOT NULL,
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
                        payload_json TEXT,
                        artifact_root TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        finished_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO verifications(
                        id,problem_id,workspace_id,kind,status,payload_json,artifact_root,created_at,finished_at
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

    def test_verification_detail_lives_in_db_without_sidecar_file(self) -> None:
        config.workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = config.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        verification_id = self.random_id("ver-detail-db")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="sig-db-only",
            kind="all",
            status="running",
            detail={
                "mode": "pass-fail",
                "pass_limit": 2,
                "selected_test_names": ["001.in", "002.in"],
                "source_paths": ["solutions/ac.cpp", "solutions/wa.cpp"],
                "sanity_checks": ["custom_sample_output"],
                "run_config_json": json.dumps({"checker_mode": "testlib", "pass_limit": 2}),
                "tests_meta_rows": [
                    {"index": 1, "kind": "manual", "desc": "manual", "source": "manual_validate.cpp"},
                    {"index": 2, "kind": "gen", "desc": "gen 2", "source": "generators/gen.cpp", "command": "2", "payload_source": "tests/2"},
                ],
            },
        )

        detail = config.verification_service.verification_detail(verification_id)
        self.assertEqual(detail.get("pass_limit"), 2)
        self.assertEqual(detail.get("selected_test_names"), ["001.in", "002.in"])
        self.assertEqual(detail.get("source_paths"), ["solutions/ac.cpp", "solutions/wa.cpp"])
        self.assertEqual(detail.get("sanity_checks"), ["custom_sample_output"])
        self.assertEqual(str(detail.get("run_config_json") or ""), json.dumps({"checker_mode": "testlib", "pass_limit": 2}))
        tests_meta_rows = detail.get("tests_meta_rows")
        self.assertIsInstance(tests_meta_rows, list)
        self.assertEqual(str((tests_meta_rows[1] or {}).get("test_name") or ""), "002.in")
        self.assertFalse((config.fs_manager.cache_artifacts_root / "verifications" / verification_id / "metadata.json").exists())

    def test_verification_artifact_refs_live_in_db_not_metadata(self) -> None:
        config.workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = config.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        verification_id = self.random_id("ver-artifact-refs-db")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="",
            kind="all",
            status="running",
            detail={"status": "running", "selected_test_names": ["001.in"]},
        )
        input_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="input",
            file_name="001.in",
            payload=b"1 2 3\n",
        )
        answer_ref = config.verification_service.store_verification_blob(
            verification_id=verification_id,
            test_name="001.in",
            role="answer",
            file_name="001.ans",
            payload=b"6\n",
        )
        config.verification_service.update_verification_artifact_refs(
            verification_id,
            "001.in",
            {"input_ref": input_ref, "answer_ref": answer_ref},
        )

        self.assertEqual(
            config.verification_service.verification_artifact_ref(verification_id, "001.in", "input_ref"),
            input_ref,
        )
        self.assertEqual(
            config.verification_service.verification_artifact_ref(verification_id, "001.in", "answer_ref"),
            answer_ref,
        )
        metadata = config.verification_service.verification_detail(verification_id)
        self.assertNotIn("artifact_refs", metadata)
        row = config.db.fetch_one(
            "SELECT input_ref,answer_ref FROM verification_artifact_refs WHERE verification_id=? AND test_name=?",
            [verification_id, "001.in"],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["input_ref"] or ""), input_ref)
        self.assertEqual(str(row["answer_ref"] or ""), answer_ref)

    def test_verification_detail_omits_redundant_runtime_fields(self) -> None:
        config.workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = config.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        verification_id = self.random_id("ver-metadata-trimmed")
        config.verification_service.begin_verification_record(
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            signature="sig-123",
            kind="all",
            status="running",
            detail={
                "mode": "pass-fail",
                "pass_limit": 1,
                "signature": "sig-123",
                "kind": "all",
                "artifact_verification_id": verification_id,
                "task_graph": True,
                "verification_source": "verification.start",
                "steps": ["gen", "val", "run", "check"],
                "selected_test_names": ["001.in"],
            },
        )
        detail = config.verification_service.verification_detail(verification_id)
        self.assertEqual(detail.get("mode"), "pass-fail")
        self.assertEqual(detail.get("pass_limit"), 1)
        self.assertEqual(detail.get("selected_test_names"), ["001.in"])
        self.assertNotIn("signature", detail)
        self.assertNotIn("kind", detail)
        self.assertNotIn("artifact_verification_id", detail)
        self.assertNotIn("task_graph", detail)
        self.assertNotIn("verification_source", detail)
        self.assertNotIn("steps", detail)
        self.assertFalse((config.fs_manager.cache_artifacts_root / "verifications" / verification_id / "metadata.json").exists())

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
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                "{}",
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
            f"/problems/{self.problem}/{self.user}/statement?language=english&preview_id={preview_id}",
            resp.headers.get("location", ""),
        )
        self.assertIn("sample verification failed.", _flash_messages_from_response(resp))

    def test_preview_page_uses_requested_language_sections(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        english_dir = ws / "statement-sections" / "english"
        chinese_dir = ws / "statement-sections" / "chinese"
        english_dir.mkdir(parents=True, exist_ok=True)
        chinese_dir.mkdir(parents=True, exist_ok=True)
        (english_dir / "legend.tex").write_text("English legend body.\n", encoding="utf-8")
        (chinese_dir / "legend.tex").write_text("Chinese legend body.\n", encoding="utf-8")

        resp = preview_page(
            _request(
                f"/problems/{self.problem}/{self.user}/statement",
                "language=chinese",
            ),
            self.problem,
            self.user,
        )

        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Chinese legend body.", html)
        self.assertIn('name="language" value="chinese"', html)
        self.assertIn('<option value="chinese" selected>', html)

    def test_statement_language_add_creates_seed_files_and_redirects_to_language(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        resp = statement_language_add(self.problem, self.user, language="japanese", page="statement")
        self.assertEqual(resp.status_code, 303)
        self.assertIn(
            f"/problems/{self.problem}/{self.user}/statement?language=japanese",
            resp.headers.get("location", ""),
        )
        for rel in (
            "name.tex",
            "legend.tex",
            "input.tex",
            "output.tex",
            "interaction.tex",
            "scoring.tex",
            "notes.tex",
        ):
            self.assertTrue((ws / "statement-sections" / "japanese" / rel).is_file(), rel)

    def test_preview_run_accepts_default_english_when_no_language_directories_exist(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        sections_root = ws / "statement-sections"
        if sections_root.exists():
            for path in sorted(sections_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            sections_root.rmdir()
        preview_id = self.random_id("p-preview-no-language-dirs")
        with patch.object(config.preview_service, "compile_preview", return_value=preview_id):
            resp = preview_run(self.problem, self.user, page="statement", language="english")
        self.assertEqual(resp.status_code, 303)
        self.assertIn(
            f"/problems/{self.problem}/{self.user}/statement?language=english&preview_id={preview_id}",
            resp.headers.get("location", ""),
        )

    def test_preview_page_shows_full_sample_build_failure_detail(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        preview_id = self.random_id("p-preview-sample-sync-detail")
        artifact_path = config.fs_manager.prepare_preview_layout(preview_id).root
        artifact_path.mkdir(parents=True, exist_ok=True)
        (artifact_path / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_path / "logs" / "latex.log").write_text(
            "sample verification failed (ver-old): validator failed on tests/spec.json entry 1\n",
            encoding="utf-8",
        )
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "failed",
                "",
                "",
                "{}",
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

    def test_preview_artifact_file_serves_statement_pdf_from_preview_root(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        preview_id = self.random_id("p-preview-artifact-pdf")
        preview_root = config.fs_manager.prepare_preview_layout(preview_id).root
        pdf_path = preview_root / "statement_preview" / "statement.pdf"
        pdf_bytes = b"%PDF-1.4\n%preview\n"
        pdf_path.write_bytes(pdf_bytes)
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                "",
                "",
                json.dumps({"pdf": "statement_preview/statement.pdf"}),
            ],
        )
        response = artifact_file(self.problem, self.user, preview_id, "statement_preview/statement.pdf")
        self.assertEqual(Path(response.path).resolve(), pdf_path.resolve())

    def test_preview_page_projects_missing_when_ok_preview_artifacts_expire(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        preview_id = self.random_id("p-preview-expired")
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                str(ctx["workspace"].get("head_commit") or ""),
                "main",
                json.dumps(
                    {
                        "pdf": "statement_preview/statement.pdf",
                        "statement_signature": statement_sources_signature(
                            ws,
                            problem_title=str(ctx["problem"]["name"]),
                        ),
                    }
                ),
            ],
        )

        resp = preview_page(
            _request(
                f"/problems/{self.problem}/{self.user}/preview",
                f"preview_id={preview_id}",
            ),
            self.problem,
            self.user,
        )
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Preview artifacts expired from cache. Recompile to regenerate the PDF and latex.log.", html)
        self.assertNotIn("PDF is ready.", html)
        self.assertNotIn("Open in a new tab", html)
        self.assertNotIn('class="pdf-preview"', html)

    def test_preview_status_projects_missing_when_ok_preview_pdf_is_gone(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        preview_id = self.random_id("p-preview-status-missing")
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                str(ctx["workspace"].get("head_commit") or ""),
                "main",
                json.dumps(
                    {
                        "pdf": "statement_preview/statement.pdf",
                        "statement_signature": statement_sources_signature(
                            ws,
                            problem_title=str(ctx["problem"]["name"]),
                        ),
                    }
                ),
            ],
        )

        resp = preview_status(self.problem, self.user)
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(str(payload.get("latest_preview_id") or ""), preview_id)
        self.assertEqual(str(payload.get("latest_status") or ""), "missing")

    def test_preview_status_can_target_explicit_language(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        chinese_dir = ws / "statement-sections" / "chinese"
        chinese_dir.mkdir(parents=True, exist_ok=True)
        preview_id = self.random_id("p-preview-status-chinese")
        preview_root = config.fs_manager.prepare_preview_layout(preview_id).root
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%zh\n")
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                str(ctx["workspace"].get("head_commit") or ""),
                "main",
                json.dumps(
                    {
                        "pdf": "statement_preview/statement.pdf",
                        "language": "chinese",
                        "statement_signature": statement_sources_signature(
                            ws,
                            problem_title=str(ctx["problem"]["name"]),
                        ),
                    }
                ),
            ],
        )

        resp = preview_status(self.problem, self.user, language="chinese")
        payload = json.loads(resp.body.decode("utf-8"))
        self.assertEqual(str(payload.get("language") or ""), "chinese")
        self.assertEqual(str(payload.get("latest_preview_id") or ""), preview_id)
        self.assertEqual(str(payload.get("latest_status") or ""), "ok")

    def test_page_ctx_projects_missing_for_latest_ok_preview_without_pdf(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        preview_id = self.random_id("p-preview-nav-missing")
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                str(ctx["workspace"].get("head_commit") or ""),
                "main",
                json.dumps(
                    {
                        "pdf": "statement_preview/statement.pdf",
                        "statement_signature": statement_sources_signature(
                            ws,
                            problem_title=str(ctx["problem"]["name"]),
                        ),
                    }
                ),
            ],
        )

        page = page_ctx(self.problem, self.user)
        preview_nav = dict(page["nav_status"]["preview"])
        self.assertEqual(str(preview_nav.get("text") or ""), "missing")
        self.assertTrue(bool(preview_nav.get("danger")))
        self.assertFalse(bool(preview_nav.get("warn")))

    def test_preview_artifact_file_reports_expired_for_missing_preview_pdf(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        preview_id = self.random_id("p-preview-artifact-expired")
        db_execute(
            (
                "INSERT INTO previews("
                "id,problem_id,workspace_id,status,source_commit,source_ref,summary_json,created_at,finished_at"
                ") VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            ),
            [
                preview_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "ok",
                "",
                "",
                json.dumps({"pdf": "statement_preview/statement.pdf"}),
            ],
        )
        with self.assertRaises(HTTPException) as exc:
            artifact_file(self.problem, self.user, preview_id, "statement_preview/statement.pdf")
        self.assertEqual(int(exc.exception.status_code), 404)
        self.assertEqual(str(exc.exception.detail or ""), "preview artifact expired")

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
