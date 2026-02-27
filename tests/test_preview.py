from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.services.sandbox.base import ExecResult
from app.services.statement_template import STATEMENT_CONTENT_TOKEN, render_statement_main
from tests.common import SmokeBase
from app.impl.config import config

preview_service = config.preview_service


class TestPreview(SmokeBase):
    def test_preview_service_and_statement_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "statement").is_dir())
        self.assertTrue((ws / "statement/template.tex").is_file())
        self.assertTrue((ws / "statement/content.tex").is_file())
        self.assertTrue((ws / "statement/olmpy.sty").is_file())
        self.assertTrue(callable(preview_service.compile_preview))

    def test_statement_template_renders_into_main_tex(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        template = (
            "\\documentclass{article}\n"
            "\\usepackage{olmpy}\n"
            "\\begin{document}\n"
            f"{STATEMENT_CONTENT_TOKEN}\n"
            "\\end{document}\n"
        )
        content = "\\Section{Problem}\nRendered content.\n"
        (statement / "template.tex").write_text(template, encoding="utf-8")
        (statement / "content.tex").write_text(content, encoding="utf-8")
        (statement / "olmpy.sty").unlink(missing_ok=True)
        out = render_statement_main(statement, problem_title="Rendered Title")
        rendered = out.read_text(encoding="utf-8")
        self.assertIn("\\usepackage{olmpy}", rendered)
        self.assertIn("\\ProblemTitle{Rendered Title}", rendered)
        self.assertIn("Rendered content.", rendered)
        self.assertNotIn(STATEMENT_CONTENT_TOKEN, rendered)
        self.assertTrue((statement / "olmpy.sty").exists())

    def test_statement_template_replaces_existing_problem_title_command(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        template = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\ProblemTitle{Old Title}\n"
            "Body.\n"
            "\\end{document}\n"
        )
        (statement / "template.tex").write_text(template, encoding="utf-8")
        (statement / "content.tex").write_text("", encoding="utf-8")
        out = render_statement_main(statement, problem_title="Preview Saved Title")
        rendered = out.read_text(encoding="utf-8")
        self.assertIn("\\ProblemTitle{Preview Saved Title}", rendered)
        self.assertNotIn("\\ProblemTitle{Old Title}", rendered)

    def test_compile_preview_skips_signatureless_cache_fallback_when_signature_available(self) -> None:
        ws = self._workspace_path()
        marker = ws / "statement" / "content.tex"
        marker.write_text(marker.read_text(encoding="utf-8") + "\n% dirty\n", encoding="utf-8")
        calls: list[tuple[object, object]] = []

        def _fake_find_cached(*args, **kwargs):
            calls.append((kwargs.get("source_commit"), kwargs.get("statement_signature")))
            if kwargs.get("statement_signature") is None:
                return "p-legacy-stale"
            return None

        with patch.object(preview_service, "_find_cached_preview_id", side_effect=_fake_find_cached), patch.object(
            preview_service.workspace_service,
            "create_snapshot",
            side_effect=RuntimeError("stop-before-compile"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-before-compile"):
                preview_service.compile_preview("sample", "alice")

        self.assertTrue(calls)
        self.assertTrue(all(signature is not None for _, signature in calls))

    def test_compile_preview_writes_pdf_from_workspace_output_without_output_directory_flag(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run(spec):
            captured["command"] = list(spec.command)
            cwd = Path(spec.cwd or ".")
            tex_name = str(spec.command[-1] or "main.tex")
            stem = Path(tex_name).stem
            (cwd / f"{stem}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            (cwd / f"{stem}.log").write_text("ok\n", encoding="utf-8")
            return ExecResult(
                backend="fake",
                status="ok",
                returncode=0,
                elapsed_ms=1,
                timed_out=False,
                stdout="",
                stderr="",
            )

        with patch.object(preview_service.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("sample", "alice")

        cmd = [str(token) for token in (captured.get("command") or [])]
        self.assertTrue(cmd)
        self.assertFalse(any(token.startswith("-output-directory=") for token in cmd))

        row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")

    def test_preview_service_reports_missing_pdflatex_format_hint(self) -> None:
        detail = preview_service._latex_compile_error_detail(
            "I can't find the format file `pdflatex.fmt'!\n",
            1,
        )
        self.assertIn("pdflatex.fmt", detail)
        self.assertIn("fmtutil -user --byfmt pdflatex", detail)
