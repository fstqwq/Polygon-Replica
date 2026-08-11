from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.impl.preview.preview import (
    preview_page,
    preview_save,
    statement_attachment_delete,
    statement_attachment_upload,
    statement_compile_asset_upload,
    statement_language_add,
    statement_language_delete,
    statement_tex_source,
)
from app.impl.runtime.config import config
from app.impl.workspace.context_ui import page_ctx
from app.config import CONFIG_REGISTRY
from app.service.statement.render import (
    default_statement_title_for_workspace,
    ensure_statement_language_sources,
)
from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.ui_support import _flash_messages_from_response, _request

TEXTAREA_MAX_BYTES = int(CONFIG_REGISTRY.defaults()["TEXTAREA_MAX_BYTES"])


class TestStatementRoutes(BackendE2ETestBase):
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
                f"/problems/{self.problem}/statement",
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
            f"/problems/{self.problem}/statement?language=japanese",
            resp.headers.get("location", ""),
        )
        for rel in (
            "name.tex",
            "legend.tex",
            "input.tex",
            "output.tex",
            "interaction.tex",
            "notes.tex",
        ):
            self.assertTrue((ws / "statement-sections" / "japanese" / rel).is_file(), rel)
        self.assertFalse((ws / "statement-sections" / "japanese" / "scoring.tex").exists())

    def test_statement_language_delete_redirects_to_remaining_language(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        ensure_statement_language_sources(ws, "chinese")

        resp = statement_language_delete(self.problem, self.user, language="chinese", page="statement")

        self.assertEqual(resp.status_code, 303)
        self.assertIn(
            f"/problems/{self.problem}/statement?language=english",
            resp.headers.get("location", ""),
        )
        self.assertFalse((ws / "statement-sections" / "chinese").exists())
        self.assertTrue((ws / "statement-sections" / "english").exists())

    def test_statement_language_delete_last_language_returns_to_missing_state(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        shutil.rmtree(ws / "statement-sections", ignore_errors=True)
        ensure_statement_language_sources(ws, "japanese")

        resp = statement_language_delete(self.problem, self.user, language="japanese", page="statement")

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location", ""), f"/problems/{self.problem}/statement")
        self.assertFalse((ws / "statement-sections" / "japanese").exists())

        page = preview_page(_request(f"/problems/{self.problem}/statement"), self.problem, self.user)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Add a language to start.", html)
        self.assertNotIn("Statement language missing.", html)
        self.assertIn("Add language", html)
        self.assertNotIn("Delete current language", html)
        self.assertNotIn("Compile Statement", html)

    def test_preview_page_shows_missing_state_until_language_is_added(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        shutil.rmtree(ws / "statement-sections", ignore_errors=True)

        resp = preview_page(_request(f"/problems/{self.problem}/statement"), self.problem, self.user)

        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Language</strong>: missing", html)
        self.assertIn("Add a language to start.", html)
        self.assertNotIn("Statement language missing.", html)
        self.assertIn("Add language", html)
        self.assertNotIn('id="statement-language-select"', html)
        self.assertNotIn("Delete Current", html)
        self.assertNotIn("Save Statement", html)
        self.assertNotIn("Compile Statement", html)

    def test_preview_page_shows_delete_current_when_language_exists(self) -> None:
        resp = preview_page(_request(f"/problems/{self.problem}/statement"), self.problem, self.user)

        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Delete current language", html)
        self.assertIn("/statement/languages/delete", html)
        self.assertNotIn("Switch to interactive mode", html)
        self.assertIn('class="form-submit-row statement-save-actions"', html)
        self.assertIn(
            'class="btn primary-action" type="submit">Save Statement</button>',
            html,
        )

    def test_preview_page_lists_compile_assets_and_contestant_attachments_separately(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        english_dir = ws / "statement-sections" / "english"
        english_dir.mkdir(parents=True, exist_ok=True)
        (english_dir / "legend.tex").write_text("Legend.\n", encoding="utf-8")
        (ws / "statement-assets").mkdir(parents=True, exist_ok=True)
        (ws / "statement-assets" / "diagram.png").write_bytes(b"PNG")
        (ws / "attachments").mkdir(parents=True, exist_ok=True)
        (ws / "attachments" / "guess_number_testing_tool.py").write_text("print('ok')\n", encoding="utf-8")

        resp = preview_page(_request(f"/problems/{self.problem}/statement"), self.problem, self.user)
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Statement attachments", html)
        self.assertIn("Contestant attachments", html)
        self.assertIn("diagram.png", html)
        self.assertIn("guess_number_testing_tool.py", html)
        self.assertIn("/statement/attachments/upload", html)
        self.assertIn("/statement/assets/upload", html)
        self.assertIn("/statement/assets/delete", html)

    def test_statement_nav_lists_language_names_when_statement_has_content(self) -> None:
        initial_page = page_ctx(self.problem, self.user)
        initial_nav = dict(initial_page["nav_status"]["statement_languages"])
        self.assertEqual(str(initial_nav.get("text") or ""), "english")

        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        ensure_statement_language_sources(ws, "chinese")
        updated_page = page_ctx(self.problem, self.user)
        updated_nav = dict(updated_page["nav_status"]["statement_languages"])
        self.assertEqual(str(updated_nav.get("text") or ""), "english, chinese")

    def test_statement_nav_ignores_legacy_scoring_section_file(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        section_root = ws / "statement-sections" / "english"
        shutil.rmtree(section_root, ignore_errors=True)
        section_root.mkdir(parents=True, exist_ok=True)
        for rel, content in {
            "name.tex": default_statement_title_for_workspace(ws) + "\n",
            "legend.tex": "",
            "input.tex": "",
            "output.tex": "",
            "interaction.tex": "",
            "notes.tex": "",
        }.items():
            (section_root / rel).write_text(content, encoding="utf-8")
        scoring_path = section_root / "scoring.tex"
        scoring_path.write_text("", encoding="utf-8")

        page = page_ctx(self.problem, self.user)
        statement_nav = dict(page["nav_status"]["statement_languages"])
        self.assertEqual(str(statement_nav.get("text") or ""), "empty")

    def test_statement_nav_shows_none_warn_when_language_directories_are_missing(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        shutil.rmtree(ws / "statement-sections", ignore_errors=True)

        page = page_ctx(self.problem, self.user)
        statement_nav = dict(page["nav_status"]["statement_languages"])
        self.assertEqual(str(statement_nav.get("text") or ""), "none")
        self.assertTrue(bool(statement_nav.get("warn")))
        self.assertFalse(bool(statement_nav.get("danger")))

    def test_statement_compile_asset_upload_stores_file_under_shared_root(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        upload = self._FakeUpload("diagram.png", b"PNG")

        resp = asyncio.run(
            statement_compile_asset_upload(
                self.problem,
                self.user,
                path="figures/diagram.png",
                upload=upload,
                page="statement",
                language="english",
            )
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{self.problem}/statement?language=english", resp.headers.get("location", ""))
        self.assertEqual((ws / "statement-assets" / "figures" / "diagram.png").read_bytes(), b"PNG")

    def test_statement_attachment_upload_stores_file_under_attachments_root(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        upload = self._FakeUpload("guess_number_testing_tool.py", b"print('ok')\n")

        resp = asyncio.run(
            statement_attachment_upload(
                self.problem,
                self.user,
                path="tools/guess_number_testing_tool.py",
                upload=upload,
                page="statement",
                language="english",
            )
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/{self.problem}/statement?language=english", resp.headers.get("location", ""))
        self.assertEqual(
            (ws / "attachments" / "tools" / "guess_number_testing_tool.py").read_text(encoding="utf-8"),
            "print('ok')\n",
        )

    def test_preview_save_rejects_statement_textarea_over_shared_limit(self) -> None:
        oversized = ("L" * (TEXTAREA_MAX_BYTES + 32)) + "\n"
        resp = preview_save(
            self.problem,
            self.user,
            legend_tex=oversized,
            input_tex="",
            output_tex="",
            interaction_tex="",
            notes_tex="",
            page="statement",
            language="english",
            preview_id="",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("statement legend is too long", _flash_messages_from_response(resp)[0])

    def test_preview_save_normalizes_textarea_newlines_to_lf(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        resp = preview_save(
            self.problem,
            self.user,
            legend_tex="Legend\r\nBody\r\n",
            input_tex="Input\r\nSection\r\n",
            output_tex="Output\r\nSection\r\n",
            interaction_tex="",
            notes_tex="Notes\r\nSection\r\n",
            page="statement",
            language="english",
            preview_id="",
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual((ws / "statement-sections" / "english" / "legend.tex").read_bytes(), b"Legend\nBody\n")
        self.assertEqual((ws / "statement-sections" / "english" / "input.tex").read_bytes(), b"Input\nSection\n")
        self.assertEqual((ws / "statement-sections" / "english" / "output.tex").read_bytes(), b"Output\nSection\n")
        self.assertEqual((ws / "statement-sections" / "english" / "notes.tex").read_bytes(), b"Notes\nSection\n")

    def test_statement_attachment_delete_removes_file_under_attachments_root(self) -> None:
        ws = Path(config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)["workspace"]["path"])
        attachment = ws / "attachments" / "guess_number_testing_tool.py"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_text("print('ok')\n", encoding="utf-8")

        resp = statement_attachment_delete(
            self.problem,
            self.user,
            path="attachments/guess_number_testing_tool.py",
            page="statement",
            language="english",
        )

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(attachment.exists())

    def test_statement_tex_source_opens_rendered_problem_tex(self) -> None:
        ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        ensure_statement_language_sources(ws, "english")
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Rendered legend for LLM.\n", encoding="utf-8")

        resp = statement_tex_source(self.problem, self.user, language="english")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", str(resp.media_type))
        self.assertIn('filename="statement-english.tex"', resp.headers.get("content-disposition", ""))
        body = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Rendered legend for LLM.", body)
        self.assertNotIn("statement_preview", body)
