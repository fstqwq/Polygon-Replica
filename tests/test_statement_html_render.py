import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.impl.preview.html import problem_statement_pdf_page
from app.impl.runtime.dependency import bind_application
from app.main import app, runtime
from app.service.disk.statement_preview_store import StatementPreviewStore
from app.service.problem.runtime_config import problem_config_limits
from app.service.sandbox.base import ExecResult
from app.service.statement.constant import DEFAULT_STATEMENT_PROBLEM_TEMPLATE
from app.service.statement.examples import StatementExamplesBundle
from app.service.statement.render import render_statement_offline_tree
from app.service.statement.tex_compile import TexCompileResult

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import suite_root


class TestStatementHtmlRender(BackendE2ETestBase):
    def test_pdf_failure_returns_the_latex_error_as_plain_text(self) -> None:
        compile_log = (
            "This is pdfTeX.\n"
            "! Undefined control sequence.\n"
            "l.42 \\BrokenStatementMacro\n"
            "No pages of output.\n"
        )
        missing_pdf = Path(suite_root()) / "missing-statement.pdf"
        compile_result = TexCompileResult(
            engine="pdflatex",
            proc=ExecResult(
                backend="fixture",
                status="failed",
                returncode=1,
                elapsed_ms=1,
            ),
            log_text=compile_log,
            pdf_path=missing_pdf,
        )
        with patch.object(
            runtime.statement_preview_service._pdf,
            "compile_pdf",
            return_value=compile_result,
        ):
            row = runtime.statement_preview_service.build_problem(
                self.problem,
                self.user,
                source_kind="workspace",
                output_kind="pdf",
                language="english",
            )

        self.assertEqual(row["status"], "failed")
        error = str(row["summary"].get("error") or "")
        self.assertTrue(error.startswith("! Undefined control sequence."))
        self.assertIn("l.42 \\BrokenStatementMacro", error)
        latex_log = (
            runtime.storage_layout.resolve_preview_root(row["id"])
            / "logs"
            / "latex.log"
        )
        self.assertEqual(latex_log.read_text(encoding="utf-8"), compile_log)

        with patch.object(
            runtime.statement_preview_service,
            "build_problem",
            return_value=row,
        ), bind_application(app):
            response = problem_statement_pdf_page(
                self.problem,
                self.user,
                source="workspace",
                language="english",
            )
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.media_type.startswith("text/plain"))
        body = response.body.decode("utf-8")
        self.assertTrue(body.startswith("! Undefined control sequence."))
        self.assertLess(
            body.index("l.42 \\BrokenStatementMacro"),
            body.index("latex.log"),
        )
        self.assertLess(body.index("latex.log"), body.index("This is pdfTeX."))
        self.assertTrue(body.endswith(compile_log))
        self.assertNotIn('{"detail":', body)

    def test_preview_records_are_scoped_to_the_requesting_user(self) -> None:
        store = StatementPreviewStore(runtime.db)
        problem_id = int(runtime.workspace_service.problem_row(self.problem)["id"])
        first_user_id = int(runtime.workspace_service.user_row(self.user)["id"])
        second_user = self.random_id("viewer")
        second_user_id = int(runtime.workspace_service.ensure_user(second_user)["id"])
        for preview_id, actor_user_id in (
            ("sp-user-one", first_user_id),
            ("sp-user-two", second_user_id),
        ):
            store.insert(
                preview_id=preview_id,
                actor_user_id=actor_user_id,
                subject_kind="problem",
                problem_id=problem_id,
                contest_id=None,
                source_kind="workspace",
                output_kind="html",
                language="english",
                input_identity="same-content",
                options={},
            )
            store.finish(preview_id, status="ok", summary={})

        first = store.latest_problem(
            problem_id,
            actor_user_id=first_user_id,
            source_kind="workspace",
            output_kind="html",
            language="english",
        )
        second = store.latest_problem(
            problem_id,
            actor_user_id=second_user_id,
            source_kind="workspace",
            output_kind="html",
            language="english",
        )
        self.assertEqual(first["id"] if first else None, "sp-user-one")
        self.assertEqual(second["id"] if second else None, "sp-user-two")
        self.assertIsNone(
            store.row("sp-user-one", actor_user_id=second_user_id)
        )

    def test_real_render_tree_converts_math_and_structured_samples(self) -> None:
        workspace = Path(
            runtime.workspace_service.workspace_context(
                self.problem,
                self.user,
                include_recent=False,
            )["workspace"]["path"]
        )
        (workspace / "statement-sections/english/legend.tex").write_text(
            "Given $a+b$, compute the value.\\[f(n)=f(n-1)+n.\\]\n"
            "\\UnknownStatementMacro{visible warning}\n",
            encoding="utf-8",
        )
        (workspace / "statement/problem.tex").write_text(
            DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
            encoding="utf-8",
        )
        bundle: StatementExamplesBundle = {
            "context": {
                "samples": [
                    {
                        "number": 1,
                        "presentation": "pair",
                        "passes": [
                            {
                                "number": 1,
                                "inputFile": "sample.1.pass.1.in",
                                "outputFile": "sample.1.pass.1.out",
                            },
                            {
                                "number": 2,
                                "inputFile": "sample.1.pass.2.in",
                                "outputFile": "sample.1.pass.2.out",
                            },
                        ],
                    },
                    {
                        "number": 2,
                        "presentation": "interaction",
                        "passes": [
                            {
                                "number": 1,
                                "events": [
                                    {
                                        "source": "interactor",
                                        "textFile": "sample.2.pass.1.event.1.txt",
                                    },
                                    {
                                        "source": "solution",
                                        "textFile": "sample.2.pass.1.event.2.txt",
                                    },
                                ],
                            },
                            {
                                "number": 2,
                                "events": [
                                    {
                                        "source": "interactor",
                                        "textFile": "sample.2.pass.2.event.1.txt",
                                    },
                                    {
                                        "source": "solution",
                                        "textFile": "sample.2.pass.2.event.2.txt",
                                    },
                                ],
                            },
                        ],
                    },
                ]
            },
            "resources": [
                {"path": "sample.1.pass.1.in", "content": "1 2\n"},
                {"path": "sample.1.pass.1.out", "content": "3\n"},
                {"path": "sample.1.pass.2.in", "content": "3 4\n"},
                {"path": "sample.1.pass.2.out", "content": "7\n"},
                {"path": "sample.2.pass.1.event.1.txt", "content": "query 1\n"},
                {"path": "sample.2.pass.1.event.2.txt", "content": "answer 1\n"},
                {"path": "sample.2.pass.2.event.1.txt", "content": "query 2\n"},
                {"path": "sample.2.pass.2.event.2.txt", "content": "answer 2\n"},
            ],
            "verification_id": "ver-html-render",
        }
        root = Path(tempfile.mkdtemp(prefix="statement-html-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_statement_offline_tree(
            workspace,
            "english",
            render_root,
            problem_title="HTML Preview Fixture",
            examples_bundle=bundle,
            tests_spec_max_bytes=runtime.config_values.integer("TEXTAREA_MAX_BYTES"),
            statement_sample_max_bytes=runtime.config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
            ),
            problem_limits=problem_config_limits(runtime.config_values),
        )

        result = runtime.statement_html_renderer.render(
            render_root,
            root / "html",
            subject_token="html-preview-fixture",
        )

        self.assertIn("<math", result.fragment)
        self.assertIn("Sample 1 Pass 1 Input", result.fragment)
        self.assertIn("Sample 1 Pass 2 Output", result.fragment)
        self.assertIn("Sample 2, Pass 1", result.fragment)
        self.assertIn("Sample 2, Pass 2", result.fragment)
        self.assertIn("query 1", result.fragment)
        self.assertIn("answer 2", result.fragment)
        self.assertNotIn(">Interactor<", result.fragment)
        self.assertNotIn(">Solution<", result.fragment)
        self.assertEqual(
            result.warnings,
            (
                "Unsupported TeX was omitted: "
                "\\UnknownStatementMacro{visible warning}",
            ),
        )

    def test_legacy_sample_environment_is_translated_at_its_source_position(self) -> None:
        workspace = Path(
            runtime.workspace_service.workspace_context(
                self.problem,
                self.user,
                include_recent=False,
            )["workspace"]["path"]
        )
        (workspace / "statement/problem.tex").write_text(
            "\\begin{problem}{${problem.name}}{${problem.inputFile}}"
            "{${problem.outputFile}}{${(problem.timeLimit / 1000)?c} seconds}"
            "{${(problem.memoryLimit / 1048576)?c} megabytes}\n"
            "${problem.legend}\n"
            "\\Examples\n"
            "\\begin{example}\n"
            "<#list problem.sampleTests as test>\n"
            "\\exmpfile{${test.inputFile}}{${test.outputFile}}%\n"
            "</#list>\n"
            "\\end{example}\n"
            "\\subsection*{After samples}\n"
            "This content follows the samples.\n"
            "\\end{problem}\n",
            encoding="utf-8",
        )
        bundle: StatementExamplesBundle = {
            "context": {
                "samples": [
                    {
                        "number": 1,
                        "presentation": "pair",
                        "passes": [
                            {
                                "number": 1,
                                "inputFile": "sample.1.pass.1.in",
                                "outputFile": "sample.1.pass.1.out",
                            }
                        ],
                    }
                ]
            },
            "resources": [
                {"path": "sample.1.pass.1.in", "content": "legacy input\n"},
                {"path": "sample.1.pass.1.out", "content": "legacy output\n"},
            ],
            "verification_id": "ver-html-legacy",
            "sample_tests": [
                {
                    "inputFile": "sample.1.pass.1.in",
                    "outputFile": "sample.1.pass.1.out",
                }
            ],
        }
        root = Path(tempfile.mkdtemp(prefix="statement-html-legacy-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_statement_offline_tree(
            workspace,
            "english",
            render_root,
            problem_title="Legacy Sample Fixture",
            examples_bundle=bundle,
            tests_spec_max_bytes=runtime.config_values.integer("TEXTAREA_MAX_BYTES"),
            statement_sample_max_bytes=runtime.config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
            ),
            problem_limits=problem_config_limits(runtime.config_values),
        )

        result = runtime.statement_html_renderer.render(
            render_root,
            root / "html",
            subject_token="html-preview-legacy",
        )

        self.assertEqual(result.warnings, ())
        self.assertIn("legacy input", result.fragment)
        self.assertIn("legacy output", result.fragment)
        self.assertLess(
            result.fragment.index("Sample 1 Input"),
            result.fragment.index("This content follows the samples."),
        )

    def test_nested_inputs_are_expanded_before_statement_macros_are_translated(
        self,
    ) -> None:
        root = Path(
            tempfile.mkdtemp(prefix="statement-html-input-", dir=suite_root())
        )
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_root.mkdir()
        (render_root / "problem.tex").write_text(
            "\\begin{problem}{Nested Input}{standard input}{standard output}"
            "{1 second}{256 megabytes}\n"
            "Before include.\n"
            "\\input{first}\n"
            "After include.\n"
            "\\end{problem}\n",
            encoding="utf-8",
        )
        (render_root / "first.tex").write_text(
            "Included prose.\n\\input{samples.tex}\n",
            encoding="utf-8",
        )
        (render_root / "samples.tex").write_text(
            "\\Example\n"
            "\\begin{example}\n"
            "\\exmpfile{sample.in}{sample.ans}%\n"
            "\\end{example}\n",
            encoding="utf-8",
        )
        (render_root / "sample.in").write_text("nested input\n", encoding="utf-8")
        (render_root / "sample.ans").write_text("nested output\n", encoding="utf-8")

        result = runtime.statement_html_renderer.render(
            render_root,
            root / "html",
            subject_token="html-preview-nested-input",
        )

        self.assertEqual(result.warnings, ())
        positions = [
            result.fragment.index(value)
            for value in (
                "Before include.",
                "Included prose.",
                "Sample 1 Input",
                "After include.",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("nested input", result.fragment)
        self.assertIn("nested output", result.fragment)

    def test_legacy_note_commands_keep_their_section_names(self) -> None:
        root = Path(
            tempfile.mkdtemp(prefix="statement-html-note-", dir=suite_root())
        )
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_root.mkdir()
        (render_root / "problem.tex").write_text(
            "\\begin{problem}{Notes}{standard input}{standard output}"
            "{1 second}{256 megabytes}\n"
            "\\Note\n"
            "A single note.\n"
            "\\Notes\n"
            "Several notes.\n"
            "\\end{problem}\n",
            encoding="utf-8",
        )

        result = runtime.statement_html_renderer.render(
            render_root,
            root / "html",
            subject_token="html-preview-note",
        )

        self.assertEqual(result.warnings, ())
        self.assertRegex(result.fragment, r"<h3[^>]*>Note</h3>")
        self.assertRegex(result.fragment, r"<h3[^>]*>Notes</h3>")
        self.assertIn("A single note.", result.fragment)
        self.assertIn("Several notes.", result.fragment)
