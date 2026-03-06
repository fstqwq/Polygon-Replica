from __future__ import annotations

import fcntl
import uuid
from pathlib import Path
from unittest.mock import patch

from app.services.sandbox.base import ExecResult
from app.services.tests_spec import dumps_tests_spec
from app.services.statement_template import (
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
    WF_STYLE_OLYMP_REL,
    WF_STYLE_STATEMENTS_REL,
    _render_ftl_template,
    render_statement_main,
    statement_sources_signature,
)
from tests.common import SmokeBase
from app.impl.config import config

preview_service = config.preview_service


class TestPreview(SmokeBase):
    def test_preview_service_and_statement_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "statement").is_dir())
        self.assertTrue((ws / STATEMENT_TEMPLATE_REL).is_file())
        self.assertTrue((ws / STATEMENT_PROBLEM_REL).is_file())
        self.assertTrue((ws / STATEMENT_STYLE_REL).is_file())
        self.assertFalse((ws / "statement" / "language.txt").exists())
        self.assertTrue((ws / "statement-sections" / "english" / "legend.tex").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "input.tex").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "output.tex").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "notes.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english" / "example.01").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "example.01.a").exists())
        self.assertEqual((ws / "statement-sections" / "english" / "legend.tex").read_text(encoding="utf-8"), "")
        self.assertEqual((ws / "statement-sections" / "english" / "input.tex").read_text(encoding="utf-8"), "")
        self.assertEqual((ws / "statement-sections" / "english" / "output.tex").read_text(encoding="utf-8"), "")
        self.assertEqual((ws / "statement-sections" / "english" / "notes.tex").read_text(encoding="utf-8"), "")
        repo_root = Path(__file__).resolve().parents[1]
        expected_ftl = (repo_root / WF_STYLE_STATEMENTS_REL).read_text(encoding="utf-8")
        expected_olymp = (repo_root / WF_STYLE_OLYMP_REL).read_text(encoding="utf-8")
        self.assertEqual((ws / STATEMENT_TEMPLATE_REL).read_text(encoding="utf-8"), expected_ftl)
        self.assertEqual((ws / STATEMENT_STYLE_REL).read_text(encoding="utf-8"), expected_olymp)
        self.assertTrue(callable(preview_service.compile_preview))

    def test_statement_template_renders_into_main_tex(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        statements_ftl = (
            "\\documentclass{article}\n"
            "\\usepackage{olymp}\n"
            "\\usepackage{import}\n"
            "\\begin{document}\n"
            "<#list statements as statement>\n"
            "\\import{${statement.path}}{./${statement.file}}\n"
            "</#list>\n"
            "\\end{document}\n"
        )
        problem_template = (
            "\\begin{problem}{${problem.name}}{${problem.inputFile}}{${problem.outputFile}}{1 second}{1 megabyte}\n"
            "${problem.legend}\n"
            "\\end{problem}\n"
        )
        (statement / "statements.ftl").write_text(statements_ftl, encoding="utf-8")
        (statement / "problem.tex").write_text(problem_template, encoding="utf-8")
        (sections / "legend.tex").write_text("Rendered content.\n", encoding="utf-8")
        out = render_statement_main(statement, problem_title="Rendered Title")
        rendered = out.read_text(encoding="utf-8")
        self.assertIn("\\usepackage{olymp}", rendered)
        self.assertIn("\\import{rendered/english/}{./problem.tex}", rendered)
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\begin{problem}{Rendered Title}", rendered_problem)
        self.assertIn("Rendered content.", rendered_problem)
        self.assertTrue((statement / "olymp.sty").exists())

    def test_statement_template_render_fails_when_olymp_style_missing(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (statement / "olymp.sty").unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, r"statement olymp style \(statement/olymp\.sty\) is missing"):
            render_statement_main(statement, problem_title="Rendered Title")

    def test_statement_template_render_fails_when_main_template_missing(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (statement / "statements.ftl").unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, r"statement template \(statement/statements\.ftl\) is missing"):
            render_statement_main(statement, problem_title="Rendered Title")

    def test_statement_template_problem_name_comes_from_problem_title(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        problem_template = (
            "\\begin{problem}{${problem.name}}{stdin}{stdout}{1 second}{1 megabyte}\n"
            "Body.\n"
            "\\end{problem}\n"
        )
        (statement / "problem.tex").write_text(problem_template, encoding="utf-8")
        out = render_statement_main(statement, problem_title="Preview Saved Title")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\begin{problem}{Preview Saved Title}", rendered_problem)

    def test_statement_template_renders_sections_when_if_condition_uses_gt_operator(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (sections / "input.tex").write_text("INPUT_MARKER_20260302\n", encoding="utf-8")
        (sections / "output.tex").write_text("OUTPUT_MARKER_20260302\n", encoding="utf-8")
        (sections / "notes.tex").write_text("NOTES_MARKER_20260302\n", encoding="utf-8")

        out = render_statement_main(statement, problem_title="Preview Saved Title")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")

        self.assertIn("\\InputFile", rendered_problem)
        self.assertIn("INPUT_MARKER_20260302", rendered_problem)
        self.assertIn("\\OutputFile", rendered_problem)
        self.assertIn("OUTPUT_MARKER_20260302", rendered_problem)
        self.assertIn("NOTES_MARKER_20260302", rendered_problem)

    def test_statement_template_samples_are_loaded_from_tests_spec_manual_entries(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (sections / "example.01").write_text("legacy-section-input\n", encoding="utf-8")
        (sections / "example.01.a").write_text("legacy-section-output\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "answers").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("manual-sample-input\n", encoding="utf-8")
        (ws / "tests" / "answers" / "001.ans").write_text("manual-sample-output\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec([{"id": "001", "kind": "manual", "sample": True}]),
            encoding="utf-8",
        )

        out = render_statement_main(statement, problem_title="Preview Saved Title")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")

        self.assertIn(r"\exmpfile{sample.001.in}{sample.001.ans}", rendered_problem)
        self.assertNotIn(r"\exmpfile{example.01}{example.01.a}", rendered_problem)
        self.assertEqual((statement / "rendered" / "english" / "sample.001.in").read_text(encoding="utf-8"), "manual-sample-input\n")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.ans").read_text(encoding="utf-8"), "manual-sample-output\n")

    def test_statement_template_samples_include_generator_entries(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (ws / "tests" / "generator").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "answers").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "generator" / "902.in").write_text("gen-sample-input\n", encoding="utf-8")
        (ws / "tests" / "answers" / "902.ans").write_text("gen-sample-output\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec([{"id": "902", "kind": "gen", "sample": True}]),
            encoding="utf-8",
        )

        out = render_statement_main(statement, problem_title="Preview Saved Title")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")

        self.assertIn(r"\exmpfile{sample.902.in}{sample.902.ans}", rendered_problem)
        self.assertEqual((statement / "rendered" / "english" / "sample.902.in").read_text(encoding="utf-8"), "gen-sample-input\n")
        self.assertEqual((statement / "rendered" / "english" / "sample.902.ans").read_text(encoding="utf-8"), "gen-sample-output\n")

    def test_statement_template_samples_prefer_custom_input_output(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "answers").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("generated-input\n", encoding="utf-8")
        (ws / "tests" / "answers" / "001.ans").write_text("generated-output\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "custom-input\n",
                        "sample_output": "custom-output\n",
                    }
                ]
            ),
            encoding="utf-8",
        )

        out = render_statement_main(statement, problem_title="Preview Saved Title")
        _ = out.read_text(encoding="utf-8")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.in").read_text(encoding="utf-8"), "custom-input\n")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.ans").read_text(encoding="utf-8"), "custom-output\n")

    def test_sample_rows_from_spec_only_requires_sync_when_custom_is_blank(self) -> None:
        ws = self._workspace_path()
        (ws / "tests").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "custom-input\n",
                        "sample_output": "custom-output\n",
                    },
                    {
                        "id": "002",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "custom-output-only\n",
                    },
                ]
            ),
            encoding="utf-8",
        )

        rows = preview_service._sample_rows_from_spec(ws)
        self.assertEqual(rows, [(2, "002", "manual")])

    def test_preview_sample_sync_materializes_manual_and_generator_from_build(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {"id": "901", "kind": "manual", "sample": True},
                    {"id": "902", "kind": "gen", "sample": True},
                ]
            ),
            encoding="utf-8",
        )
        build_id = self.random_id("b-preview-sample-sync")
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "tests" / "001.in").write_text("build-manual-input\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("build-manual-answer\n", encoding="utf-8")
        (artifact_root / "tests" / "002.in").write_text("build-gen-input\n", encoding="utf-8")
        (artifact_root / "ans" / "002.ans").write_text("build-gen-answer\n", encoding="utf-8")

        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        config.db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )

        calls: list[tuple[str, str, bool]] = []

        class _FakeBuildService:
            def run_build(
                self,
                problem: str,
                username: str,
                commit=None,
                ref=None,
                *,
                prefer_local_solve_backend: bool = False,
                sample_only: bool = False,
            ):
                calls.append((str(problem), str(username), bool(prefer_local_solve_backend)))
                return build_id

        old_build_service = preview_service.build_service
        try:
            preview_service.build_service = _FakeBuildService()
            summary = preview_service._copy_sample_payloads_from_build("alice/sample", "alice", ws)
        finally:
            preview_service.build_service = old_build_service

        self.assertEqual(calls, [("alice/sample", "alice", True)])
        self.assertEqual(int(summary.get("copied") or 0), 2)
        self.assertEqual((ws / "tests" / "manual" / "901.in").read_text(encoding="utf-8"), "build-manual-input\n")
        self.assertEqual((ws / "tests" / "answers" / "901.ans").read_text(encoding="utf-8"), "build-manual-answer\n")
        self.assertEqual((ws / "tests" / "generator" / "902.in").read_text(encoding="utf-8"), "build-gen-input\n")
        self.assertEqual((ws / "tests" / "answers" / "902.ans").read_text(encoding="utf-8"), "build-gen-answer\n")

    def test_compile_preview_with_samples_skips_cache_and_syncs_samples(self) -> None:
        ws = self._workspace_path()
        calls = {"find_cached": 0, "sync": 0}

        def _fake_find_cached(*_args, **_kwargs):
            calls["find_cached"] = int(calls["find_cached"]) + 1
            return None

        def _fake_sync(problem: str, user: str, snapshot: Path):
            _ = (problem, user, snapshot)
            lock_path = ws / ".polygonlike.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_fh:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise AssertionError("workspace lock must not be held during sample sync") from exc
                finally:
                    try:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            calls["sync"] = int(calls["sync"]) + 1
            return {"sample_count": 1, "copied": 1, "build_id": "b-sync"}

        def _fake_run(spec):
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

        with patch.object(preview_service, "_sample_rows_from_spec", return_value=[(1, "901", "manual")]), patch.object(
            preview_service,
            "find_cached_preview_id",
            side_effect=_fake_find_cached,
        ), patch.object(
            preview_service,
            "_copy_sample_payloads_from_build",
            side_effect=_fake_sync,
        ), patch.object(preview_service.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice")

        self.assertEqual(int(calls["find_cached"]), 0)
        self.assertEqual(int(calls["sync"]), 1)
        row = config.db.fetch_one("SELECT status,summary_json FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        self.assertIn("sample_sync", str(row["summary_json"] or ""))

    def test_render_ftl_strips_standalone_directive_lines(self) -> None:
        rendered = _render_ftl_template(
            "A\n<#list problem.sampleTests as test>\nX${test.inputFile}\n</#list>\nB\n",
            {"problem": {"sampleTests": [{"inputFile": "1"}, {"inputFile": "2"}]}},
        )
        self.assertEqual(rendered, "A\nX1\nX2\nB\n")

    def test_statement_template_sets_short_problem_title_in_main_template_context(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (statement / "statements.ftl").write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "<#if shortProblemTitle?? && shortProblemTitle>\n"
            "\\def\\ShortProblemTitle{}\n"
            "</#if>\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
        (statement / "problem.tex").write_text("\\begin{problem}{${problem.name}}{stdin}{stdout}{1 second}{1 megabyte}\\end{problem}\n", encoding="utf-8")
        out = render_statement_main(statement, problem_title="Preview Title")
        rendered = out.read_text(encoding="utf-8")
        self.assertIn("\\def\\ShortProblemTitle{}", rendered)

    def test_compile_preview_skips_signatureless_cache_fallback_when_signature_available(self) -> None:
        ws = self._workspace_path()
        marker = ws / "statement" / "problem.tex"
        marker.write_text(marker.read_text(encoding="utf-8") + "\n% dirty\n", encoding="utf-8")
        calls: list[tuple[object, object]] = []

        def _fake_find_cached(*args, **kwargs):
            calls.append((kwargs.get("source_commit"), kwargs.get("statement_signature")))
            if kwargs.get("statement_signature") is None:
                return "p-legacy-stale"
            return None

        with patch.object(preview_service, "find_cached_preview_id", side_effect=_fake_find_cached), patch.object(
            preview_service.workspace_service,
            "create_snapshot",
            side_effect=RuntimeError("stop-before-compile"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-before-compile"):
                preview_service.compile_preview("alice/sample", "alice")

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
            preview_id = preview_service.compile_preview("alice/sample", "alice")

        cmd = [str(token) for token in (captured.get("command") or [])]
        self.assertTrue(cmd)
        self.assertFalse(any(token.startswith("-output-directory=") for token in cmd))

        row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")

    def test_compile_preview_runs_pdflatex_twice_on_success(self) -> None:
        calls = {"count": 0}

        def _fake_run(spec):
            calls["count"] = int(calls["count"]) + 1
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

        with patch.object(preview_service, "tex_passes", 2), patch.object(preview_service.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice")

        self.assertEqual(int(calls["count"]), 2)
        row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")

    def test_preview_service_reports_missing_pdflatex_format_hint(self) -> None:
        detail = preview_service._latex_compile_error_detail(
            "I can't find the format file `pdflatex.fmt'!\n",
            1,
        )
        self.assertIn("pdflatex.fmt", detail)
        self.assertIn("fmtutil", detail)

    def test_preview_service_reports_missing_latex_package_name(self) -> None:
        detail = preview_service._latex_compile_error_detail(
            "! LaTeX Error: File `siunitx.sty' not found.\n",
            1,
        )
        self.assertIn("missing LaTeX package", detail)
        self.assertIn("siunitx.sty", detail)

    def test_compile_preview_failed_run_still_writes_nonempty_latex_log(self) -> None:
        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            tex_name = str(spec.command[-1] or "main.tex")
            stem = Path(tex_name).stem
            # Simulate TeX generating an empty log and no PDF on failure.
            (cwd / f"{stem}.log").write_text("", encoding="utf-8")
            return ExecResult(
                backend="fake",
                status="error",
                returncode=1,
                elapsed_ms=1,
                timed_out=False,
                stdout="",
                stderr="",
            )

        with patch.object(preview_service.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice")

        row = config.db.fetch_one("SELECT status,artifact_path,summary_json FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        artifact_root = Path(str(row["artifact_path"] or ""))
        log_path = artifact_root / "logs" / "latex.log"
        self.assertTrue(log_path.exists())
        log_text = log_path.read_text(encoding="utf-8", errors="replace").strip()
        self.assertTrue(log_text)
        self.assertIn("latex compile failed", log_text.lower())

    def test_compile_preview_failure_does_not_emit_prebuilt_pdf(self) -> None:
        ws = self._workspace_path()
        prebuilt = ws / "statement" / "rendered" / "english" / "problem.pdf"
        prebuilt.parent.mkdir(parents=True, exist_ok=True)
        prebuilt.write_bytes(b"%PDF-1.4\n% prebuilt\n%%EOF\n")

        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            tex_name = str(spec.command[-1] or "main.tex")
            stem = Path(tex_name).stem
            (cwd / f"{stem}.log").write_text("! undefined control sequence\n", encoding="utf-8")
            return ExecResult(
                backend="fake",
                status="error",
                returncode=1,
                elapsed_ms=1,
                timed_out=False,
                stdout="",
                stderr="",
            )

        with patch.object(preview_service.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice")

        row = config.db.fetch_one("SELECT status,artifact_path,summary_json FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        artifact_root = Path(str(row["artifact_path"] or ""))
        self.assertFalse((artifact_root / "statement_preview" / "statement.pdf").exists())

    def test_statement_signature_changes_when_gen_sample_payload_changes(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "generator").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "answers").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "generator" / "901.in").write_text("gen-a\n", encoding="utf-8")
        (ws / "tests" / "answers" / "901.ans").write_text("ans-a\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec([{"id": "901", "kind": "gen", "sample": True}]),
            encoding="utf-8",
        )
        before = statement_sources_signature(ws, problem_title="T")
        (ws / "tests" / "generator" / "901.in").write_text("gen-b\n", encoding="utf-8")
        after = statement_sources_signature(ws, problem_title="T")
        self.assertNotEqual(before, after)

    def test_preview_sample_rows_invalid_kind_raises(self) -> None:
        ws = self._workspace_path()
        (ws / "tests").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "spec.json").write_text(
            '{"version":2,"tests":[{"id":"001","kind":"generator","sample":true}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid tests/spec.json"):
            preview_service._sample_rows_from_spec(ws)

    def test_prune_workspace_preview_history_keeps_running_rows(self) -> None:
        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        keep_id = f"p-{uuid.uuid4().hex[:12]}"
        running_id = f"p-{uuid.uuid4().hex[:12]}"
        done_id = f"p-{uuid.uuid4().hex[:12]}"
        keep_root = self._artifact_root(keep_id)
        running_root = self._artifact_root(running_id)
        done_root = self._artifact_root(done_id)
        keep_root.mkdir(parents=True, exist_ok=True)
        running_root.mkdir(parents=True, exist_ok=True)
        done_root.mkdir(parents=True, exist_ok=True)
        config.db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [keep_id, problem_id, workspace_id, "", "main", "ok", "{}", str(keep_root), "2026-03-05T00:00:00Z"],
        )
        config.db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [running_id, problem_id, workspace_id, "", "main", "running", "{}", str(running_root), "2026-03-05T00:00:00Z"],
        )
        config.db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [done_id, problem_id, workspace_id, "", "main", "failed", "{}", str(done_root), "2026-03-05T00:00:00Z"],
        )

        preview_service.prune_workspace_preview_history("alice/sample", problem_id, workspace_id, keep_id)

        running_row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [running_id])
        done_row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [done_id])
        self.assertIsNotNone(running_row)
        self.assertEqual(str(running_row["status"] or ""), "running")
        self.assertIsNone(done_row)

    def test_prune_workspace_preview_history_keeps_shared_artifact_root_for_kept_row(self) -> None:
        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        keep_id = f"p-{uuid.uuid4().hex[:12]}"
        done_id = f"p-{uuid.uuid4().hex[:12]}"
        shared_root = self._artifact_root(self.random_id("preview-shared"))
        shared_root.mkdir(parents=True, exist_ok=True)
        (shared_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (shared_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        for preview_id, status in ((keep_id, "ok"), (done_id, "failed")):
            config.db.execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [preview_id, problem_id, workspace_id, "", "main", status, "{}", str(shared_root), "2026-03-05T00:00:00Z"],
            )

        preview_service.prune_workspace_preview_history("alice/sample", problem_id, workspace_id, keep_id)

        keep_row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [keep_id])
        done_row = config.db.fetch_one("SELECT status FROM previews WHERE id=?", [done_id])
        self.assertIsNotNone(keep_row)
        self.assertEqual(str(keep_row["status"] or ""), "ok")
        self.assertIsNone(done_row)
        self.assertTrue(shared_root.exists())
        self.assertTrue((shared_root / "statement_preview" / "statement.pdf").exists())

