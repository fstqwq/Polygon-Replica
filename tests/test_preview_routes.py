from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.impl.preview.preview import (
    preview_page,
    preview_run,
    preview_status,
)
from app.impl.run_export.artifact import artifact_file
from app.impl.runtime.config import config
from app.impl.workspace.context_ui import page_ctx
from app.service.statement.signature import statement_sources_signature

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.db_helpers import db_execute, write_preview_summary
from tests.ui_support import _flash_messages_from_response, _request


class TestPreviewRoutes(BackendE2ETestBase):
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
            f"/problems/{self.problem}/statement?language=english",
            resp.headers.get("location", ""),
        )
        self.assertIn("sample verification failed.", _flash_messages_from_response(resp))

    def test_preview_run_rejects_missing_language_directories(self) -> None:
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
        with patch.object(config.preview_service, "compile_preview", return_value=preview_id) as compile_preview:
            resp = preview_run(self.problem, self.user, page="statement", language="english")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location", ""), f"/problems/{self.problem}/statement")
        self.assertEqual(_flash_messages_from_response(resp), ["statement language is missing."])
        compile_preview.assert_not_called()

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
                f"/problems/{self.problem}/statement",
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
                            problem_title=self._statement_title(ws),
                            tests_spec_max_bytes=int(
                                config.config_values.TEXTAREA_MAX_BYTES
                            ),
                            statement_sample_max_bytes=int(
                                config.config_values.STATEMENT_SAMPLE_MAX_BYTES
                            ),
                        ),
                    }
                ),
            ],
        )

        resp = preview_page(
            _request(
                f"/problems/{self.problem}/preview",
                f"preview_id={preview_id}",
            ),
            self.problem,
            self.user,
        )
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Recompile to restore the preview.", html)
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
                            problem_title=self._statement_title(ws),
                            tests_spec_max_bytes=int(
                                config.config_values.TEXTAREA_MAX_BYTES
                            ),
                            statement_sample_max_bytes=int(
                                config.config_values.STATEMENT_SAMPLE_MAX_BYTES
                            ),
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
                            problem_title=self._statement_title(ws, "chinese"),
                            tests_spec_max_bytes=int(
                                config.config_values.TEXTAREA_MAX_BYTES
                            ),
                            statement_sample_max_bytes=int(
                                config.config_values.STATEMENT_SAMPLE_MAX_BYTES
                            ),
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

    def test_page_ctx_does_not_project_preview_state_from_latest_preview_row(self) -> None:
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
                            problem_title=self._statement_title(ws),
                            tests_spec_max_bytes=int(
                                config.config_values.TEXTAREA_MAX_BYTES
                            ),
                            statement_sample_max_bytes=int(
                                config.config_values.STATEMENT_SAMPLE_MAX_BYTES
                            ),
                        ),
                    }
                ),
            ],
        )

        with patch.object(config.preview_service, "get_workspace_preview_state", side_effect=AssertionError("preview state lookup should stay local to statement page")):
            page = page_ctx(self.problem, self.user)
        self.assertNotIn("preview", page["nav_status"])

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
        self.assertIn(f"/problems/{self.problem}/statement", resp.headers.get("location", ""))
