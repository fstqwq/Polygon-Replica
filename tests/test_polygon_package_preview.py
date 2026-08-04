from __future__ import annotations

from unittest.mock import patch

from app.service.statement.render import render_statement_main

from tests.common import E2ETestBase, config
from tests.db_helpers import db_fetch_one, read_preview_summary


class TestPolygonPackagePreview(E2ETestBase):
    def test_preview_fails_when_pdflatex_is_missing(self) -> None:
        workspace = self._workspace_path()
        render_statement_main(
            workspace / "statement",
            problem_title="Sample Problem",
            language="english",
        )
        with (
            patch.object(
                config.preview_service,
                "_sample_verification_rows_from_spec",
                return_value=[],
            ),
            patch.object(
                config.tex_compile_service.sandbox,
                "run",
                side_effect=FileNotFoundError("pdflatex missing"),
            ),
        ):
            preview_id = config.preview_service.compile_preview(
                self.problem,
                self.user,
                language="english",
            )

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertEqual(row["status"], "failed")
        summary = read_preview_summary(preview_id)
        self.assertIn("pdflatex missing", summary["error"])
        artifact_root = config.fs_manager.resolve_preview_root(preview_id)
        self.assertFalse((artifact_root / "statement_preview/statement.pdf").exists())
