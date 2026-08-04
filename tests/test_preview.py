from __future__ import annotations

from tests.db_helpers import (
    db_execute,
    db_fetch_one,
    read_preview_summary,
    write_preview_summary,
)

import base64
import json
import fcntl
import os
import uuid
from pathlib import Path
from unittest.mock import patch

from app.service.sandbox.base import ExecResult
from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.statement.constant import (
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    STATEMENT_ASSETS_DIR,
    STATEMENT_PROBLEM_REL,
    STATEMENT_STYLE_REL,
    STATEMENT_TEMPLATE_REL,
)
from app.impl.workspace.sample_output_validation import validate_custom_sample_outputs
from app.service.verification.plan import VerificationTestPlan
from app.service.statement.context import pick_statement_language, statement_languages
from app.service.statement.ftl.renderer import render_ftl_template
from app.service.statement.render import render_statement_main
from app.service.statement.signature import statement_sources_signature
from app.service.verification.signature import verification_fingerprint, verification_signature
from tests.common import E2ETestBase
from app.impl.runtime.config import config

preview_service = config.preview_service


class TestPreview(E2ETestBase):
    seed_default_workspace = True

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
        self.assertEqual((ws / "statement-sections" / "english" / "legend.tex").read_text(encoding="utf-8"), "Legend.\n")
        self.assertEqual((ws / "statement-sections" / "english" / "input.tex").read_text(encoding="utf-8"), "Input.\n")
        self.assertEqual((ws / "statement-sections" / "english" / "output.tex").read_text(encoding="utf-8"), "Output.\n")
        self.assertEqual((ws / "statement-sections" / "english" / "notes.tex").read_text(encoding="utf-8"), "")
        self.assertIn("\\input{rendered/english/problem.tex}", (ws / STATEMENT_TEMPLATE_REL).read_text(encoding="utf-8"))
        self.assertIn("${problem.legend}", (ws / STATEMENT_PROBLEM_REL).read_text(encoding="utf-8"))
        self.assertIn("minimal olymp style for tests", (ws / STATEMENT_STYLE_REL).read_text(encoding="utf-8"))
        self.assertTrue(callable(preview_service.compile_preview))

    def test_statement_languages_sort_english_then_chinese_then_alphabetical(self) -> None:
        ws = self._workspace_path()
        root = ws / "statement-sections"
        (root / "japanese").mkdir(parents=True, exist_ok=True)
        (root / "arabic").mkdir(parents=True, exist_ok=True)
        (root / "chinese").mkdir(parents=True, exist_ok=True)
        self.assertEqual(statement_languages(ws), ["english", "chinese", "arabic", "japanese"])
        self.assertEqual(pick_statement_language(ws), "english")

    def test_find_cached_preview_id_uses_db_artifact_fallback(self) -> None:
        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        def _insert_preview(
            preview_id: str,
            *,
            language: str,
            created_at: str,
            write_artifacts: bool = True,
        ) -> None:
            if write_artifacts:
                layout = config.fs_manager.prepare_preview_layout(preview_id)
                (layout.statement_preview / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
                (layout.logs / "latex.log").write_text("ok\n", encoding="utf-8")
            db_execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [preview_id, problem_id, workspace_id, "head-123", "main", "ok", "{}", created_at],
            )
            write_preview_summary(
                preview_id,
                {
                    "pdf": "statement_preview/statement.pdf",
                    "statement_signature": "sig-123",
                    "language": language,
                },
            )

        english_id = f"p-{uuid.uuid4().hex[:12]}"
        chinese_id = f"p-{uuid.uuid4().hex[:12]}"
        missing_latest_id = f"p-{uuid.uuid4().hex[:12]}"
        _insert_preview(english_id, language="english", created_at="2026-04-11T00:00:00Z")
        _insert_preview(chinese_id, language="chinese", created_at="2026-04-11T00:00:01Z")
        _insert_preview(
            missing_latest_id,
            language="english",
            created_at="2026-04-11T00:00:02Z",
            write_artifacts=False,
        )

        resolved_chinese = preview_service.find_cached_preview_id(
            "alice/sample",
            problem_id,
            workspace_id,
            language="chinese",
            source_commit="head-123",
            statement_signature="sig-123",
        )
        self.assertEqual(resolved_chinese, chinese_id)

        resolved_english = preview_service.find_cached_preview_id(
            "alice/sample",
            problem_id,
            workspace_id,
            language="english",
            source_commit="head-123",
            statement_signature="sig-123",
        )
        self.assertEqual(resolved_english, english_id)

        resolved_stale = preview_service.find_cached_preview_id(
            "alice/sample",
            problem_id,
            workspace_id,
            language="english",
            source_commit="head-123",
            statement_signature="sig-other",
        )
        self.assertIsNone(resolved_stale)

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
        (sections / "name.tex").write_text("Rendered Title\n", encoding="utf-8")
        (sections / "legend.tex").write_text("Rendered content.\n", encoding="utf-8")
        out = render_statement_main(statement, problem_title="Rendered Title", language="english")
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
            render_statement_main(statement, problem_title="Rendered Title", language="english")

    def test_statement_template_render_fails_when_main_template_missing(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (statement / "statements.ftl").unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, r"statement template \(statement/statements\.ftl\) is missing"):
            render_statement_main(statement, problem_title="Rendered Title", language="english")

    def test_statement_template_problem_name_prefers_name_tex(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (ws / "statement-sections" / "english" / "name.tex").write_text("Language Title\n", encoding="utf-8")
        problem_template = (
            "\\begin{problem}{${problem.name}}{stdin}{stdout}{1 second}{1 megabyte}\n"
            "Body.\n"
            "\\end{problem}\n"
        )
        (statement / "problem.tex").write_text(problem_template, encoding="utf-8")
        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\begin{problem}{Language Title}", rendered_problem)

    def test_statement_render_uses_shared_assets_and_ignores_legacy_section_extras(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (sections / "legacy-only.txt").write_text("legacy\n", encoding="utf-8")
        assets = ws / STATEMENT_ASSETS_DIR
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "figures").mkdir(parents=True, exist_ok=True)
        (assets / "figures" / "diagram.png").write_bytes(b"PNG")

        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
        _ = out.read_text(encoding="utf-8")
        rendered_root = statement / "rendered" / "english"
        self.assertTrue((rendered_root / "figures" / "diagram.png").is_file())
        self.assertFalse((rendered_root / "legacy-only.txt").exists())

    def test_statement_template_renders_sections_when_if_condition_uses_gt_operator(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (statement / "problem.tex").write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (sections / "input.tex").write_text("INPUT_MARKER_20260302\n", encoding="utf-8")
        (sections / "output.tex").write_text("OUTPUT_MARKER_20260302\n", encoding="utf-8")
        (sections / "notes.tex").write_text("NOTES_MARKER_20260302\n", encoding="utf-8")

        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
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
        (statement / "problem.tex").write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (sections / "example.01").write_text("legacy-section-input\n", encoding="utf-8")
        (sections / "example.01.a").write_text("legacy-section-output\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("manual-sample-input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [{"id": "001", "kind": "manual", "sample": True, "sample_output": "manual-sample-output\n"}]
            ),
            encoding="utf-8",
        )

        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
        _ = out.read_text(encoding="utf-8")
        rendered_problem = (statement / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")

        self.assertIn(r"\exmpfile{sample.001.in}{sample.001.ans}", rendered_problem)
        self.assertNotIn(r"\exmpfile{example.01}{example.01.a}", rendered_problem)
        self.assertEqual((statement / "rendered" / "english" / "sample.001.in").read_text(encoding="utf-8"), "manual-sample-input\n")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.ans").read_text(encoding="utf-8"), "manual-sample-output\n")

    def test_statement_template_samples_include_generator_entries(self) -> None:
        ws = self._workspace_path()
        statement = ws / "statement"
        (statement / "problem.tex").write_text(DEFAULT_STATEMENT_PROBLEM_TEMPLATE, encoding="utf-8")
        sections = ws / "statement-sections" / "english"
        sections.mkdir(parents=True, exist_ok=True)
        (sections / "legend.tex").write_text("Legend marker.\n", encoding="utf-8")
        (ws / "tests" / "generator").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "generator" / "902.in").write_text("gen-sample-input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec([{"id": "902", "kind": "gen", "sample": True, "sample_output": "gen-sample-output\n"}]),
            encoding="utf-8",
        )

        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
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
        (ws / "tests" / "manual" / "001.in").write_text("generated-input\n", encoding="utf-8")
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

        out = render_statement_main(statement, problem_title="Preview Saved Title", language="english")
        _ = out.read_text(encoding="utf-8")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.in").read_text(encoding="utf-8"), "custom-input\n")
        self.assertEqual((statement / "rendered" / "english" / "sample.001.ans").read_text(encoding="utf-8"), "custom-output\n")

    def test_sample_verification_rows_include_validate_only_custom_output(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("base-input-1\n", encoding="utf-8")
        (ws / "tests" / "manual" / "002.in").write_text("base-input-2\n", encoding="utf-8")
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
                        "sample_input": "custom-input-only\n",
                    },
                ]
            ),
            encoding="utf-8",
        )

        rows = preview_service._sample_verification_rows_from_spec(ws)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].index, 1)
        self.assertEqual(rows[0].test_id, "001")
        self.assertFalse(rows[0].needs_input_copy)
        self.assertFalse(rows[0].needs_output_copy)
        self.assertTrue(rows[0].validate_custom_output)
        self.assertEqual(rows[1].index, 2)
        self.assertEqual(rows[1].test_id, "002")
        self.assertFalse(rows[1].needs_input_copy)
        self.assertTrue(rows[1].needs_output_copy)
        self.assertFalse(rows[1].validate_custom_output)

    def test_sample_verification_rows_skip_fully_materialized_unvalidated_samples(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("base-input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "display-output\n",
                        "sample_output_validate": False,
                    }
                ]
            ),
            encoding="utf-8",
        )

        rows = preview_service._sample_verification_rows_from_spec(ws)
        self.assertEqual(rows, [])

    def test_generation_params_digest_changes_when_build_sources_change(self) -> None:
        ws = self._workspace_path()
        digest_before = verification_signature(ws)

        testlib_path = ws / "third_party" / "testlib" / "testlib.h"
        testlib_path.write_text(
            testlib_path.read_text(encoding="utf-8") + "\n// digest probe\n",
            encoding="utf-8",
        )
        digest_after_testlib = verification_signature(ws)
        self.assertNotEqual(digest_before, digest_after_testlib)

        validator_path = ws / "validators" / "validator.cpp"
        validator_path.write_text(
            '#include "testlib.h"\nint main(int argc, char* argv[]) { registerValidation(argc, argv); inf.readEof(); }\n',
            encoding="utf-8",
        )
        digest_after_validator = verification_signature(ws)
        self.assertNotEqual(digest_after_testlib, digest_after_validator)

    def test_verification_signature_ignores_mtime_when_content_is_unchanged(self) -> None:
        ws = self._workspace_path()
        target = ws / "config" / "build.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        digest_before = verification_signature(ws)
        fingerprint_before = verification_fingerprint(ws)

        stat_obj = target.stat()
        os.utime(
            target,
            ns=(
                int(stat_obj.st_atime_ns) + 5_000_000_000,
                int(stat_obj.st_mtime_ns) + 5_000_000_000,
            ),
        )

        self.assertEqual(digest_before, verification_signature(ws))
        self.assertNotEqual(fingerprint_before, verification_fingerprint(ws))

    def test_preview_sample_sync_builds_manual_and_generator_from_verification(self) -> None:
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
        verification_id = self.random_id("ver-preview-sample-sync")
        runtime_layout = config.fs_manager.prepare_verification_runtime_layout(verification_id)
        (runtime_layout.tests / "001.in").write_text("build-manual-input\n", encoding="utf-8")
        (runtime_layout.answers / "001.ans").write_text("build-manual-answer\n", encoding="utf-8")
        (runtime_layout.tests / "002.in").write_text("build-gen-input\n", encoding="utf-8")
        (runtime_layout.answers / "002.ans").write_text("build-gen-answer\n", encoding="utf-8")

        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "",
                "all",
                "ok",
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )

        calls: list[tuple[str, str]] = []

        class _FakeVerificationService:
            def run_verification(
                self,
                problem: str,
                username: str,
                commit=None,
                ref=None,
                *,
                sample_only: bool = False,
            ):
                calls.append((str(problem), str(username)))
                return verification_id

            def verification_artifact_ref(self, verification_id_arg: str, test_name: str, ref_key: str) -> str:
                _ = verification_id_arg
                if test_name == "001.in":
                    return f"blob://{ref_key}/001"
                if test_name == "002.in":
                    return f"blob://{ref_key}/002"
                return ""

            def resolve_artifact_blob(self, token: str) -> bytes | None:
                payloads = {
                    "blob://input_ref/001": b"build-manual-input\n",
                    "blob://answer_ref/001": b"build-manual-answer\n",
                    "blob://input_ref/002": b"build-gen-input\n",
                    "blob://answer_ref/002": b"build-gen-answer\n",
                }
                return payloads.get(token)

        old_verification_service = preview_service.verification_service
        try:
            preview_service.verification_service = _FakeVerificationService()
            summary = preview_service._copy_sample_payloads_from_verification("alice/sample", "alice", ws)
        finally:
            preview_service.verification_service = old_verification_service

        self.assertEqual(calls, [("alice/sample", "alice")])
        self.assertEqual(int(summary.get("copied") or 0), 2)
        self.assertEqual((ws / "tests" / "manual" / "901.in").read_text(encoding="utf-8"), "build-manual-input\n")
        self.assertEqual((ws / "tests" / "generator" / "902.in").read_text(encoding="utf-8"), "build-gen-input\n")
        tests_spec = load_tests_spec(ws / "tests" / "spec.json")
        self.assertEqual(str(tests_spec[0].get("sample_output") or ""), "build-manual-answer\n")
        self.assertEqual(str(tests_spec[1].get("sample_output") or ""), "build-gen-answer\n")
        self.assertFalse((ws / "tests" / "answers").exists())

    def test_preview_sample_sync_keeps_manual_payload_when_custom_sample_input_present(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "901.in").write_text("base-manual-input\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "901",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "custom-sample-input\n",
                    }
                ]
            ),
            encoding="utf-8",
        )
        verification_id = self.random_id("ver-preview-custom-sample")
        runtime_layout = config.fs_manager.prepare_verification_runtime_layout(verification_id)
        (runtime_layout.tests / "001.in").write_text("custom-sample-input\n", encoding="utf-8")
        (runtime_layout.answers / "001.ans").write_text("custom-sample-answer\n", encoding="utf-8")

        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                "",
                "all",
                "ok",
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )

        class _FakeVerificationService:
            def run_verification(
                self,
                problem: str,
                username: str,
                commit=None,
                ref=None,
                *,
                sample_only: bool = False,
            ):
                _ = (problem, username, commit, ref, sample_only)
                return verification_id

            def verification_artifact_ref(self, verification_id_arg: str, test_name: str, ref_key: str) -> str:
                _ = verification_id_arg
                if test_name == "001.in":
                    return f"blob://{ref_key}/001"
                return ""

            def resolve_artifact_blob(self, token: str) -> bytes | None:
                payloads = {
                    "blob://input_ref/001": b"custom-sample-input\n",
                    "blob://answer_ref/001": b"custom-sample-answer\n",
                }
                return payloads.get(token)

        old_verification_service = preview_service.verification_service
        try:
            preview_service.verification_service = _FakeVerificationService()
            summary = preview_service._copy_sample_payloads_from_verification("alice/sample", "alice", ws)
        finally:
            preview_service.verification_service = old_verification_service

        self.assertEqual(int(summary.get("copied") or 0), 1)
        self.assertEqual((ws / "tests" / "manual" / "901.in").read_text(encoding="utf-8"), "base-manual-input\n")
        tests_spec = load_tests_spec(ws / "tests" / "spec.json")
        self.assertEqual(str(tests_spec[0].get("sample_output") or ""), "custom-sample-answer\n")
        self.assertFalse((ws / "tests" / "answers").exists())

    def test_preview_sample_sync_skips_interactive_samples(self) -> None:
        ws = self._workspace_path()
        (ws / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "mode": "interactive",
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 1024,
                    "pass_limit": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "901",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "play\n3\n00\n1 3",
                        "sample_output": "take\nignore",
                        "sample_output_validate": True,
                    }
                ]
            ),
            encoding="utf-8",
        )

        class _FailingVerificationService:
            def run_verification(self, *_args, **_kwargs):
                raise AssertionError("interactive sample sync must not run verification")

        old_verification_service = preview_service.verification_service
        try:
            preview_service.verification_service = _FailingVerificationService()
            rows = preview_service._sample_verification_rows_from_spec(ws)
            summary = preview_service._copy_sample_payloads_from_verification("alice/sample", "alice", ws)
        finally:
            preview_service.verification_service = old_verification_service

        self.assertEqual(rows, [])
        self.assertEqual(int(summary.get("sample_count") or 0), 0)
        self.assertEqual(str(summary.get("skipped") or ""), "interactive")

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
            return {"sample_count": 1, "copied": 1, "verification_id": ""}

        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(
            preview_service,
            "_sample_verification_rows_from_spec",
            return_value=[
                preview_service._SampleVerificationRow(
                    index=1,
                    test_id="901",
                    kind="manual",
                    needs_input_copy=True,
                    needs_output_copy=True,
                    validate_custom_output=False,
                )
            ],
        ), patch.object(
            preview_service,
            "find_cached_preview_id",
            side_effect=_fake_find_cached,
        ), patch.object(
            preview_service,
            "_copy_sample_payloads_from_verification",
            side_effect=_fake_sync,
        ), patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        self.assertEqual(int(calls["find_cached"]), 0)
        self.assertEqual(int(calls["sync"]), 1)
        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        self.assertIn("sample_sync", json.dumps(read_preview_summary(preview_id)))

    def test_validate_custom_sample_outputs_uses_exact_diff_without_checker(self) -> None:
        verification_id = self.random_id("ver-validate-sample")
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        logs_root = artifact_root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        plan = VerificationTestPlan(
            test_name="001.in",
            source_kind="manual",
            display_source_path="manual_validate.cpp",
            execution_source_name="manual_validate.cpp",
            execution_source_bytes=b"int main(){return 0;}\n",
            execution_input_bytes=b"1\n",
            extra_sources_b64={},
            tests_meta={},
            sample=True,
            sample_input_custom=False,
            sample_input_text="",
            uses_custom_sample_input=False,
            sample_output_text="ok\n",
            sample_output_validate=True,
        )
        calls: list[tuple[str, list[str]]] = []

        def _fake_enqueue_task(**kwargs):
            calls.append((str(kwargs["upload_filename"]), list(kwargs["selected_tests"])))
            return "jt-sanity-ok"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            self.assertEqual(task_id, "jt-sanity-ok")
            self.assertEqual(test_name, "001.in")
            return {
                "status": "ok",
                "error": "",
                "summary": {
                    "tests": [
                        {
                            "test": "001.in",
                            "verdict": "OK",
                            "message": "",
                        }
                    ]
                },
            }

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ):
            result = validate_custom_sample_outputs(
                problem="alice/sample",
                user="alice",
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_root,
                test_plans=[plan],
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_count, 1)
        self.assertEqual(calls, [("custom_sample_output.py", ["001.in"])])
        self.assertEqual((logs_root / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n")

    def test_validate_custom_sample_outputs_uses_custom_input_without_sample_only(self) -> None:
        verification_id = self.random_id("ver-validate-sample-custom-input")
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        logs_root = artifact_root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        plan = VerificationTestPlan(
            test_name="001.in",
            source_kind="manual",
            display_source_path="manual_validate.cpp",
            execution_source_name="manual_validate.cpp",
            execution_source_bytes=b"int main(){return 0;}\n",
            execution_input_bytes=b"base\n",
            extra_sources_b64={},
            tests_meta={},
            sample=True,
            sample_input_custom=True,
            sample_input_text="custom-input\n",
            uses_custom_sample_input=False,
            sample_output_text="custom-answer\n",
            sample_output_validate=True,
        )
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs):
            calls.append(dict(kwargs))
            return "jt-main-ok" if len(calls) == 1 else "jt-sanity-ok"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            self.assertEqual(test_name, "001.in")
            if task_id == "jt-main-ok":
                return {
                    "status": "ok",
                    "error": "",
                    "summary": {
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "OK",
                                "message": "",
                                "output_ref": "cache://answer",
                            }
                        ]
                    },
                }
            self.assertEqual(task_id, "jt-sanity-ok")
            return {
                "status": "ok",
                "error": "",
                "summary": {
                    "tests": [
                        {
                            "test": "001.in",
                            "verdict": "OK",
                            "message": "",
                        }
                    ]
                },
            }

        def _fake_case_output(task_id: str, test_name: str):
            self.assertEqual(task_id, "jt-main-ok")
            self.assertEqual(test_name, "001.in")
            return ("cache://answer", None, 1)

        def _fake_resolve_artifact_blob(output_ref: str, *, work_root: object = None) -> bytes | None:
            self.assertEqual(output_ref, "cache://answer")
            self.assertIsNone(work_root)
            return b"custom-answer\n"

        payload_base = {
            "run_config_json": "{}",
            "problem_limits": {"time_limit_ms": 2000, "memory_limit_mb": 1024, "pass_limit": 1},
            "binaries_b64": {},
            "sources_b64": {},
        }
        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            config.judgehost_task_service,
            "domjudge_case_output_for_task",
            side_effect=_fake_case_output,
        ), patch.object(
            config.judgehost_task_service,
            "resolve_artifact_blob",
            side_effect=_fake_resolve_artifact_blob,
        ):
            result = validate_custom_sample_outputs(
                problem="alice/sample",
                user="alice",
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_root,
                test_plans=[plan],
                accepted_source_label="solutions/std.cpp",
                accepted_source_name="std.cpp",
                accepted_source_bytes=b"int main(){return 0;}\n",
                run_verification_payload_base=payload_base,
                bypass_case_result_cache=True,
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_count, 1)
        self.assertEqual([str(call["upload_filename"]) for call in calls], ["std.cpp", "custom_sample_output.py"])
        self.assertEqual(str(calls[0]["verification_source"]), "main-correct")
        self.assertEqual(str(calls[0]["task_kind"]), "main-correct")
        self.assertTrue(all(call["bypass_case_result_cache"] is True for call in calls))
        first_payload = dict(calls[0]["prepared_payload"])
        second_payload = dict(calls[1]["prepared_payload"])
        first_test = list(dict(first_payload["verification_payload"])["tests"])[0]
        second_test = list(dict(second_payload["verification_payload"])["tests"])[0]
        self.assertEqual(first_test["input_b64"], base64.b64encode(b"custom-input\n").decode("ascii"))
        self.assertEqual(first_test["answer_b64"], "")
        self.assertEqual(second_test["input_b64"], base64.b64encode(b"custom-input\n").decode("ascii"))
        self.assertEqual(second_test["answer_b64"], base64.b64encode(b"custom-answer\n").decode("ascii"))
        self.assertEqual((logs_root / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n")

    def test_render_ftl_strips_standalone_directive_lines(self) -> None:
        rendered = render_ftl_template(
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
        out = render_statement_main(statement, problem_title="Preview Title", language="english")
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
                preview_service.compile_preview("alice/sample", "alice", language="english")

        self.assertTrue(calls)
        self.assertTrue(all(signature is not None for _, signature in calls))

    def test_compile_preview_writes_pdf_from_workspace_output_without_output_directory_flag(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run(spec):
            captured["command"] = [str(token) for token in spec.command]
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        cmd = [str(token) for token in (captured.get("command") or [])]
        self.assertTrue(cmd)
        self.assertFalse(any(token.startswith("-output-directory=") for token in cmd))

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")

    def test_compile_preview_runs_pdflatex_twice_on_success(self) -> None:
        calls = {"count": 0}

        def _fake_run(spec):
            calls["count"] = int(calls["count"]) + 1
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler, "passes", 2), patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        self.assertEqual(int(calls["count"]), 2)
        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")

    def test_compile_preview_stores_null_verification_id_without_sample_sync(self) -> None:
        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        row = db_fetch_one("SELECT verification_id FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertIsNone(row["verification_id"])

    def test_compile_preview_success_summary_contract_fields_stable(self) -> None:
        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = read_preview_summary(preview_id)
        self.assertEqual(str(summary.get("pdf") or ""), "statement_preview/statement.pdf")
        self.assertTrue(str(summary.get("statement_signature") or "").strip())
        self.assertTrue(str(summary.get("preview_ref") or "").strip())
        self.assertEqual(str(summary.get("language") or ""), "english")
        self.assertNotIn("error", summary)

    def test_compile_preview_failure_summary_contract_fields_stable(self) -> None:
        marker = "statement/main.tex:7 Undefined control sequence"

        def _fake_run(spec):
            cwd = Path(spec.cwd or ".")
            stem = Path(str(spec.command[-1])).stem
            (cwd / f"{stem}.log").write_text(marker + "\n", encoding="utf-8")
            return ExecResult(
                backend="fake",
                status="error",
                returncode=1,
                elapsed_ms=1,
                timed_out=False,
                stdout="",
                stderr="",
            )

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = read_preview_summary(preview_id)
        self.assertTrue(str(summary.get("error") or "").strip())
        self.assertEqual(int(summary.get("returncode") or 0), 1)
        self.assertTrue(str(summary.get("statement_signature") or "").strip())
        self.assertTrue(str(summary.get("preview_ref") or "").strip())
        self.assertEqual(str(summary.get("language") or ""), "english")
        log_text = (config.fs_manager.resolve_preview_root(preview_id) / "logs" / "latex.log").read_text(
            encoding="utf-8",
            errors="replace",
        )
        self.assertIn(marker, log_text)

    def test_preview_service_reports_missing_pdflatex_format_hint(self) -> None:
        detail = preview_service._latex_compile_error_detail(
            "I can't find the format file `pdflatex.fmt'!\n",
            1,
        )
        self.assertIn("missing LaTeX format file", detail)
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
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        artifact_root = config.fs_manager.resolve_preview_root(preview_id)
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
            stem = Path(str(spec.command[-1])).stem
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

        with patch.object(preview_service.pdf_compiler.sandbox, "run", side_effect=_fake_run):
            preview_id = preview_service.compile_preview("alice/sample", "alice", language="english")

        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        artifact_root = config.fs_manager.resolve_preview_root(preview_id)
        self.assertFalse((artifact_root / "statement_preview" / "statement.pdf").exists())

    def test_statement_signature_changes_when_gen_sample_payload_changes(self) -> None:
        ws = self._workspace_path()
        (ws / "tests" / "generator").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "generator" / "901.in").write_text("gen-a\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            dumps_tests_spec([{"id": "901", "kind": "gen", "sample": True, "sample_output": "ans-a\n"}]),
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
            preview_service._sample_verification_rows_from_spec(ws)

    def test_prune_workspace_preview_history_keeps_running_rows(self) -> None:
        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        keep_id = f"p-{uuid.uuid4().hex[:12]}"
        running_id = f"p-{uuid.uuid4().hex[:12]}"
        done_id = f"p-{uuid.uuid4().hex[:12]}"
        keep_root = config.fs_manager.prepare_preview_layout(keep_id).root
        running_root = config.fs_manager.prepare_preview_layout(running_id).root
        done_root = config.fs_manager.prepare_preview_layout(done_id).root
        keep_root.mkdir(parents=True, exist_ok=True)
        running_root.mkdir(parents=True, exist_ok=True)
        done_root.mkdir(parents=True, exist_ok=True)
        db_execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [keep_id, problem_id, workspace_id, "", "main", "ok", "{}", "2026-03-05T00:00:00Z"],
        )
        write_preview_summary(keep_id, {})
        db_execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [running_id, problem_id, workspace_id, "", "main", "running", "{}", "2026-03-05T00:00:00Z"],
        )
        write_preview_summary(running_id, {})
        db_execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [done_id, problem_id, workspace_id, "", "main", "failed", "{}", "2026-03-05T00:00:00Z"],
        )
        write_preview_summary(done_id, {})

        preview_service.prune_workspace_preview_history("alice/sample", problem_id, workspace_id, keep_id)

        running_row = db_fetch_one("SELECT status FROM previews WHERE id=?", [running_id])
        done_row = db_fetch_one("SELECT status FROM previews WHERE id=?", [done_id])
        self.assertIsNotNone(running_row)
        self.assertEqual(str(running_row["status"] or ""), "running")
        self.assertIsNone(done_row)

    def test_prune_workspace_preview_history_keeps_shared_artifact_root_for_kept_row(self) -> None:
        ctx = preview_service.workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        keep_id = f"p-{uuid.uuid4().hex[:12]}"
        done_id = f"p-{uuid.uuid4().hex[:12]}"
        keep_root = config.fs_manager.prepare_preview_layout(keep_id).root
        done_root = config.fs_manager.prepare_preview_layout(done_id).root
        keep_root.mkdir(parents=True, exist_ok=True)
        done_root.mkdir(parents=True, exist_ok=True)
        (keep_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (keep_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (done_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (done_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        for preview_id, status in ((keep_id, "ok"), (done_id, "failed")):
            db_execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [preview_id, problem_id, workspace_id, "", "main", status, "{}", "2026-03-05T00:00:00Z"],
            )
            write_preview_summary(preview_id, {})

        preview_service.prune_workspace_preview_history("alice/sample", problem_id, workspace_id, keep_id)

        keep_row = db_fetch_one("SELECT status FROM previews WHERE id=?", [keep_id])
        done_row = db_fetch_one("SELECT status FROM previews WHERE id=?", [done_id])
        self.assertIsNotNone(keep_row)
        self.assertEqual(str(keep_row["status"] or ""), "ok")
        self.assertIsNone(done_row)
        self.assertTrue(keep_root.exists())
        self.assertTrue((keep_root / "statement_preview" / "statement.pdf").exists())
        self.assertFalse(done_root.exists())
