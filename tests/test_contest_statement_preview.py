from unittest.mock import patch

from app.service.sandbox.base import ExecResult, ExecSpec
from app.service.statement.constant import DEFAULT_STATEMENT_TEMPLATE

from tests.contest_support import ContestActionBase
from tests.ui_support import runtime


class TestContestStatementPreview(ContestActionBase):
    def test_language_resolution_requires_an_available_fallback(self) -> None:
        slug, contest_id, owner_id = self.create_contest("statement-language")
        for language in runtime.contest_statement_service.languages(contest_id):
            runtime.contest_service.delete_statement_language_sources(
                contest_id=contest_id, contest_slug=slug, language=language
            )
        runtime.contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=slug,
            actor_user_id=owner_id,
            key="statements/chinese/statements.ftl",
            package_bytes=DEFAULT_STATEMENT_TEMPLATE.encode("utf-8"),
        )
        self.assertEqual(runtime.contest_statement_service.resolve_language(contest_id), "chinese")
        runtime.contest_service.delete_statement_language_sources(
            contest_id=contest_id, contest_slug=slug, language="chinese"
        )
        self.assertEqual(runtime.contest_statement_service.resolve_language(contest_id), "")

    def test_pdf_preview_compiles_and_caches_one_complete_contest_document(self) -> None:
        slug, contest_id, owner_id = self.create_contest("complete-contest")
        runtime.contest_service.set_properties(contest_id, owner_id, {
            "banner": r"\textbf{Preview only}",
            "insertBlankPage": "true",
            "title": "Complete Contest",
            "location": "Hangzhou",
            "date": "19 August 2026",
            "sponsor": "Example Foundation",
        })
        for index, title in (("A", "Problem A"), ("B", "Problem B")):
            _, _, problem_slug = self.add_owned_problem(contest_id, owner_id, index, f"preview-{index.lower()}")
            workspace = self._seed_workspace(problem_slug, "alice")
            (workspace / "statement-sections/english/name.tex").write_text(title, encoding="utf-8")
        sources = {
            "statements/english/statements.ftl": DEFAULT_STATEMENT_TEMPLATE.replace(
                r"\begin {document}",
                "% sponsor=${sponsor!}\n% nested-sponsor=${properties.sponsor!}\n\\begin {document}",
            ),
            "statements/_shared/shared-resource.txt": "shared\n",
            "statements/_shared/overridden.txt": "shared\n",
            "statements/english/overridden.txt": "english\n",
        }
        for key, content in sources.items():
            runtime.contest_service.write_statement_source_file(
                contest_id=contest_id, contest_slug=slug, actor_user_id=owner_id,
                key=key, package_bytes=content.encode("utf-8"),
            )
        compiled_sources: dict[str, str] = {}

        def compile_pdf(spec: ExecSpec) -> ExecResult:
            assert spec.cwd is not None
            entrypoint = spec.cwd / spec.command[-1]
            compiled_sources["entrypoint"] = entrypoint.read_text(encoding="utf-8")
            for resource in ("shared-resource.txt", "overridden.txt"):
                compiled_sources[resource] = (spec.cwd / resource).read_text(encoding="utf-8")
            for source in spec.cwd.parents[1].glob("problems/*/statements/english/problem.tex"):
                compiled_sources[source.parent.parent.parent.name] = source.read_text(encoding="utf-8")
            entrypoint.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n% complete contest\n")
            entrypoint.with_suffix(".log").write_text("complete\n", encoding="utf-8")
            return ExecResult(backend="fixture", status="ok", returncode=0, elapsed_ms=1)

        with patch.object(runtime.tex_sandbox_backend, "run", side_effect=compile_pdf):
            first = runtime.contest_statement_preview_service.build_pdf(
                contest_id, contest_slug=slug, user_id=owner_id, username="alice",
                source_kind="workspace", language="english",
            )
        self.assertEqual(first["status"], "ok", first["summary"])
        second = runtime.contest_statement_preview_service.build_pdf(
            contest_id, contest_slug=slug, user_id=owner_id, username="alice",
            source_kind="workspace", language="english",
        )
        self.assertEqual(second["id"], first["id"])
        pdf = runtime.statement_preview_service.pdf(second["id"], actor_user_id=owner_id)
        self.assertIsNotNone(pdf)
        assert pdf is not None
        self.assertEqual(pdf.read_bytes(), b"%PDF-1.4\n% complete contest\n")
        self.assertEqual(compiled_sources["shared-resource.txt"], "shared\n")
        self.assertEqual(compiled_sources["overridden.txt"], "english\n")
        document = compiled_sources["entrypoint"]
        for text in (
            r"\intentionallyblankpagestrue", r"\renewcommand{\StatementBanner}",
            r"\usepackage {hyperref}", r"\textbf{Preview only}",
            "% sponsor=Example Foundation", "% nested-sponsor=Example Foundation",
        ):
            self.assertIn(text, document)
        results = second["summary"]["results"]
        self.assertEqual([row["idx"] for row in results], ["A", "B"])
        for row in results:
            self.assertIn(r"\def\ProblemIndex{" + row["idx"] + "}", document)
            self.assertIn("../../problems/" + row["source_folder"] + "/statements/english/", document)
            self.assertIn("Problem " + row["idx"], compiled_sources[row["source_folder"]])
        self.assertNotIn("<#", document)

    def test_pdf_failure_preserves_error_context_and_complete_latex_log(self) -> None:
        slug, contest_id, owner_id = self.create_contest("broken-contest")
        _, _, problem_slug = self.add_owned_problem(contest_id, owner_id, "A", "broken-preview")
        workspace = self._seed_workspace(problem_slug, "alice")
        (workspace / "statement-sections/english/legend.tex").write_text("\\BrokenContestMacro\n", encoding="utf-8")
        log_text = (
            "This is XeTeX.\nentering extended mode\n"
            "! Undefined control sequence.\nl.19 \\BrokenContestMacro\nNo pages of output.\n"
        )

        def failed_tex(spec: ExecSpec) -> ExecResult:
            assert spec.cwd is not None
            (spec.cwd / spec.command[-1]).with_suffix(".log").write_text(log_text, encoding="utf-8")
            return ExecResult(
                backend="fixture", status="failed", returncode=1, elapsed_ms=1,
                stderr="xelatex stopped with an error\n",
            )

        with patch.object(runtime.tex_sandbox_backend, "run", side_effect=failed_tex):
            preview = runtime.contest_statement_preview_service.build_pdf(
                contest_id, contest_slug=slug, user_id=owner_id, username="alice",
                source_kind="workspace", language="english",
            )
        self.assertEqual(preview["status"], "failed")
        self.assertTrue(preview["summary"]["error"].startswith("! Undefined control sequence."))
        self.assertIn("l.19 \\BrokenContestMacro", preview["summary"]["error"])
        self.assertIsNone(runtime.statement_preview_service.pdf(preview["id"], actor_user_id=owner_id))
        self.assertEqual(
            runtime.statement_preview_service.latex_log(preview["id"], actor_user_id=owner_id),
            log_text,
        )
