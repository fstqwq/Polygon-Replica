import shutil
import tempfile
from pathlib import Path

from app.main import runtime
from app.service.disk.statement_preview_store import StatementPreviewStore
from app.service.problem.runtime_config import problem_config_limits
from app.service.statement.examples import StatementExamplesBundle
from app.service.statement.render import render_statement_offline_tree

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import suite_root


class TestStatementHtmlRender(BackendE2ETestBase):
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
        self.assertTrue(
            any("UnknownStatementMacro" in warning for warning in result.warnings)
        )
