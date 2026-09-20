import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.impl.preview.html import (
    problem_statement_html_page,
    problem_statement_pdf_page,
)
from app.impl.runtime.dependency import bind_application
from app.main import app, runtime
from app.service.disk.statement_preview_store import StatementPreviewStore
from app.service.problem.build_config import dumps_build_config
from app.service.problem.runtime_config import problem_config_limits
from app.service.sandbox.base import ExecResult, ExecSpec
from app.service.statement.constant import DEFAULT_STATEMENT_PROBLEM_TEMPLATE
from app.service.statement.examples import StatementExamplesBundle
from app.service.statement.html_render import number_statement_fragment
from app.service.statement.render import render_statement_offline_tree

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import suite_root
from tests.db_helpers import db_fetch_one
from tests.ui_support import _request


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.images: list[dict[str, str | None]] = []
        self._cell: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            self._cell = []
            self._in_cell = True
        elif tag == "img":
            self.images.append(dict(attrs))

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._in_cell = False


class _HeadingCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[tuple[str, dict[str, str | None], str]] = []
        self._tag = ""
        self._attributes: dict[str, str | None] = {}
        self._text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"h2", "h3"}:
            self._tag = tag
            self._attributes = dict(attrs)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._tag:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._tag:
            self.headings.append(
                (
                    self._tag,
                    self._attributes,
                    "".join(self._text).strip(),
                )
            )
            self._tag = ""
            self._attributes = {}
            self._text = []


def _headings(fragment: str) -> list[tuple[str, dict[str, str | None], str]]:
    parser = _HeadingCollector()
    parser.feed(fragment)
    parser.close()
    return parser.headings


class TestStatementHtmlRender(BackendE2ETestBase):
    @staticmethod
    def _compile_pdf(spec: ExecSpec) -> ExecResult:
        if spec.cwd is None:
            raise AssertionError("TeX requires a compile directory")
        source = spec.cwd / spec.command[-1]
        source.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n% compiled fixture\n")
        return ExecResult(backend="fixture", status="ok", returncode=0, elapsed_ms=1)

    def test_parbox_captions_stay_in_their_table_cells_with_inline_images(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="statement-table-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_root.mkdir()
        image_names = ("red-hearts.png", "blue-hearts.png", "red-heart-icon.png", "soul-heart-icon.png")
        for name in image_names:
            (render_root / name).write_bytes(b"PNG fixture")
        source = r"""\begin{problem}{Table layout}{stdin}{stdout}{1 second}{256 megabytes}
\begin{center}
\begin{tabular}{@{}c@{\qquad}c@{}}
\includegraphics[width=0.27\textwidth]{red-hearts.png} &
\includegraphics[width=0.27\textwidth]{blue-hearts.png} ROWBREAK
\parbox[t]{0.27\textwidth}{\centering Pay one \includegraphics[width=0.8em]{red-heart-icon.png}.} &
\parbox[t]{0.27\textwidth}{\centering Pay up to three \includegraphics[width=0.8em]{soul-heart-icon.png}.}
\end{tabular}
\end{center}
\end{problem}
"""
        for breaks in (2, 4):
            with self.subTest(row_break_backslashes=breaks):
                (render_root / "problem.tex").write_text(
                    source.replace("ROWBREAK", "\\" * breaks), encoding="utf-8",
                )
                result = runtime.statement_html_renderer.render(
                    render_root, root / f"html-{breaks}", subject_token=f"parbox-{breaks}",
                )
                table = _TableCollector()
                table.feed(result.fragment)
                self.assertEqual(table.rows[-1], ["Pay one .", "Pay up to three ."])
                self.assertEqual(len(result.resources), 4)
                self.assertEqual(
                    [image["style"] for image in table.images],
                    ["width:27cqw", "width:27cqw", "width:0.8em", "width:0.8em"],
                )
                self.assertEqual(result.warnings, ())

    def test_problem_reader_can_render_own_workspace_html_and_pdf(self) -> None:
        reader = "statement-reader"
        workspace = self._seed_workspace(self.problem, reader)
        runtime.workspace_service.grant_repo_access(self.problem, reader, "read")
        (workspace / "statement-sections/english/legend.tex").write_text(
            "Reader workspace preview.\n", encoding="utf-8"
        )

        with bind_application(app):
            html_response = problem_statement_html_page(
                _request(f"/problems/{self.problem}/statement/html"),
                self.problem,
                reader,
                source="workspace",
                language="english",
            )
        with patch.object(runtime.tex_sandbox_backend, "run", side_effect=self._compile_pdf), bind_application(app):
            pdf_response = problem_statement_pdf_page(
                self.problem,
                reader,
                source="workspace",
                language="english",
            )

        self.assertEqual(html_response.status_code, 200)
        self.assertIn("Reader workspace preview.", html_response.body.decode("utf-8"))
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(
            Path(pdf_response.path).read_bytes(), b"%PDF-1.4\n% compiled fixture\n"
        )

    def test_cached_html_serves_the_sanitized_renderer_output(self) -> None:
        def pandoc(spec: ExecSpec) -> ExecResult:
            output = next(arg.removeprefix("--output=") for arg in spec.command if arg.startswith("--output="))
            content = '{"blocks":[]}' if "--to=json" in spec.command else (
                '<h2>Safe statement</h2><p><script>alert(1)</script>'
                '<img src="local.png" onerror="alert(2)" style="width:27cqw;position:fixed">'
                '<a href="javascript:alert(3)">caption</a></p>'
            )
            Path(output).write_text(content, encoding="utf-8")
            return ExecResult(backend="fixture", status="ok", returncode=0, elapsed_ms=1)

        service = runtime.statement_preview_service
        user_id = int(runtime.workspace_service.user_row(self.user)["id"])
        with patch.object(runtime.tex_sandbox_backend, "run", side_effect=pandoc):
            first = service.build_problem(
                self.problem, self.user, source_kind="workspace", output_kind="html", language="english"
            )
        second = service.build_problem(
            self.problem, self.user, source_kind="workspace", output_kind="html", language="english"
        )
        self.assertEqual(first["id"], second["id"])
        fragment = service.html_fragment(second["id"], actor_user_id=user_id)
        self.assertIsNotNone(fragment)
        assert fragment is not None
        self.assertIn("Safe statement", fragment)
        self.assertIn("caption", fragment)
        images = _TableCollector()
        images.feed(fragment)
        self.assertEqual(images.images, [{"src": "local.png", "style": "width:27cqw"}])
        self.assertNotIn("<script", fragment)
        self.assertNotIn("javascript:", fragment)

    def test_native_package_preview_rejects_package_from_another_problem(
        self,
    ) -> None:
        foreign_problem = f"{self.user}/foreign-statement"
        runtime.workspace_service.ensure_problem(foreign_problem)
        foreign_problem_id = runtime.workspace_service.problem_row(foreign_problem)["id"]
        package_id = self._package_record(foreign_problem_id)
        with bind_application(app):
            with self.assertRaises(HTTPException) as error:
                problem_statement_html_page(
                    _request(f"/problems/{self.problem}/statement/html"),
                    self.problem,
                    self.user,
                    source="native_package",
                    language="english",
                    native_package_id=package_id,
                )

        self.assertEqual(error.exception.status_code, 404)
        self.assertIsNone(db_fetch_one("SELECT id FROM statement_previews"))

    @staticmethod
    def _package_record(problem_id: int) -> str:
        package_id = "pm-statement-fixture"
        runtime.problem_package_service.store.insert_materialization(
            {
                "id": package_id,
                "problem_id": problem_id,
                "source_commit": "a" * 40,
                "revision_number": 1,
                "source_digest": "b" * 64,
                "archive_rel_path": "statement-fixture.zip",
                "archive_sha256": "c" * 64,
                "archive_size_bytes": 1,
                "verification_id": "",
                "status": "available",
                "created_at": "2026-01-01T00:00:00Z",
                "checked_at": "2026-01-01T00:00:00Z",
                "unavailable_reason": "",
            },
            build_id="",
        )
        return package_id

    def test_native_package_publication_busy_is_reported_as_conflict(self) -> None:
        problem_id = runtime.workspace_service.problem_row(self.problem)["id"]
        package_id = self._package_record(problem_id)
        with runtime.problem_package_service._archive_publication(problem_id, "a" * 40), bind_application(app):
            with self.assertRaises(HTTPException) as html_error:
                problem_statement_html_page(
                    _request(f"/problems/{self.problem}/statement/html"),
                    self.problem,
                    self.user,
                    source="native_package",
                    language="english",
                    native_package_id=package_id,
                )
            with self.assertRaises(HTTPException) as pdf_error:
                problem_statement_pdf_page(
                    self.problem,
                    self.user,
                    source="native_package",
                    language="english",
                    native_package_id=package_id,
                )

        self.assertEqual(html_error.exception.status_code, 409)
        self.assertEqual(pdf_error.exception.status_code, 409)
        package = runtime.problem_package_service.native_package(package_id)
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(package["status"], "available")

    def test_dynamic_statement_examples_use_foreground_verification(self) -> None:
        workspace = Path(
            runtime.workspace_service.workspace_context(
                self.problem,
                self.user,
                include_recent=False,
            )["workspace"]["path"]
        )
        manual_root = workspace / "tests" / "manual"
        manual_root.mkdir(parents=True, exist_ok=True)
        (manual_root / "001.in").write_text("1\n", encoding="utf-8")
        (workspace / "tests" / "spec.json").write_text(
            '{"tests":[{"id":"001","kind":"manual","sample":true}]}\n',
            encoding="utf-8",
        )
        (workspace / "solutions/main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (workspace / "config/build.json").write_text(
            dumps_build_config({"generator_sources": [], "accepted_solution_source": "solutions/main.cpp"}),
            encoding="utf-8",
        )
        runtime.judgehost_task_service.domjudge_register_host("statement-foreground")
        service_classes: list[str] = []

        def unavailable_executor(*, service_class: str, **_kwargs: object) -> str:
            service_classes.append(service_class)
            raise RuntimeError("sample executor unavailable")

        with patch.object(runtime.judgehost_task_service, "enqueue_task", side_effect=unavailable_executor), bind_application(app):
            response = problem_statement_html_page(
                _request(f"/problems/{self.problem}/statement/html"),
                self.problem,
                self.user,
                source="workspace",
                language="english",
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("sample executor unavailable", response.body.decode("utf-8"))
        self.assertEqual(set(service_classes), {"foreground"})
        problem_id = int(runtime.workspace_service.problem_row(self.problem)["id"])
        record = db_fetch_one(
            "SELECT id,kind,status FROM verifications WHERE problem_id=?", [problem_id]
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual((record["kind"], record["status"]), ("sample", "failed"))
        self.assertEqual(
            runtime.verification_service.verification_detail(record["id"])["selected_test_names"],
            ["001.in"],
        )

    def test_preview_preparation_failure_returns_diagnostic_response(self) -> None:
        workspace = Path(
            runtime.workspace_service.workspace_context(
                self.problem,
                self.user,
                include_recent=False,
            )["workspace"]["path"]
        )
        (workspace / "statement/olymp.sty").unlink()
        failure = "statement olymp style (statement/olymp.sty) is missing"

        with bind_application(app):
            html_response = problem_statement_html_page(
                _request(f"/problems/{self.problem}/statement/html"),
                self.problem,
                self.user,
                source="workspace",
                language="english",
            )
            pdf_response = problem_statement_pdf_page(
                self.problem,
                self.user,
                source="workspace",
                language="english",
            )

        self.assertEqual(html_response.status_code, 422)
        self.assertIn(failure, html_response.body.decode("utf-8"))
        self.assertEqual(pdf_response.status_code, 422)
        self.assertIn(failure, pdf_response.body.decode("utf-8"))

    def test_problem_and_contest_html_reuse_source_identity_cache(self) -> None:
        workspace = Path(
            runtime.workspace_service.workspace_context(
                self.problem,
                self.user,
                include_recent=False,
            )["workspace"]["path"]
        )
        (workspace / "statement-sections/english/legend.tex").write_text(
            "Cache identity fixture.\n",
            encoding="utf-8",
        )
        user_id = int(runtime.workspace_service.user_row(self.user)["id"])
        problem_id = int(runtime.workspace_service.problem_row(self.problem)["id"])
        contest_slug = self.random_id("statement-cache")
        contest_id = runtime.contest_service.create_contest_with_owner(
            slug=contest_slug,
            title="Statement Cache",
            owner_user_id=user_id,
        )
        runtime.contest_service.add_problem(
            contest_id,
            "A",
            problem_id,
            user_id,
        )

        first = runtime.contest_statement_preview_service.build_html(
            contest_id,
            user_id=user_id,
            username=self.user,
            source_kind="workspace",
            language="english",
        )
        second = runtime.contest_statement_preview_service.build_html(
            contest_id,
            user_id=user_id,
            username=self.user,
            source_kind="workspace",
            language="english",
        )

        self.assertEqual(first["id"], second["id"])
        item = runtime.contest_statement_preview_service.items(second)[0]
        self.assertIn(
            "Cache identity fixture.",
            runtime.statement_preview_service.html_fragment(item["preview_id"], actor_user_id=user_id),
        )
        with patch.object(
            runtime.tex_sandbox_backend,
            "run",
            side_effect=self._compile_pdf,
        ):
            first_pdf = runtime.contest_statement_preview_service.build_pdf(
                contest_id,
                contest_slug=contest_slug,
                user_id=user_id,
                username=self.user,
                source_kind="workspace",
                language="english",
            )
        second_pdf = runtime.contest_statement_preview_service.build_pdf(
            contest_id,
            contest_slug=contest_slug,
            user_id=user_id,
            username=self.user,
            source_kind="workspace",
            language="english",
        )

        self.assertEqual(first_pdf["id"], second_pdf["id"])
        pdf = runtime.statement_preview_service.pdf(second_pdf["id"], actor_user_id=user_id)
        self.assertIsNotNone(pdf)
        assert pdf is not None
        self.assertEqual(pdf.read_bytes(), b"%PDF-1.4\n% compiled fixture\n")

    def test_html_preview_rebuilds_missing_and_unsafe_cached_payloads(self) -> None:
        service = runtime.statement_preview_service
        user_id = int(runtime.workspace_service.user_row(self.user)["id"])
        row = service.build_problem(
            self.problem,
            self.user,
            source_kind="workspace",
            output_kind="html",
            language="english",
            native_package_id="pm-ignored-for-workspace",
        )
        fragment = service.html_fragment(row["id"], actor_user_id=user_id)
        self.assertTrue(fragment)
        outside = Path(tempfile.mkdtemp(prefix="preview-outside-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "content.html").write_text("outside preview", encoding="utf-8")

        for unavailable in ("missing", "symlink", "outside-parent"):
            with self.subTest(payload=unavailable):
                html_root = runtime.storage_layout.resolve_preview_root(row["id"]) / "html"
                payload = html_root / "content.html"
                payload.unlink()
                if unavailable == "symlink":
                    payload.symlink_to(outside / "content.html")
                elif unavailable == "outside-parent":
                    shutil.rmtree(html_root)
                    html_root.symlink_to(outside, target_is_directory=True)
                self.assertIsNone(service.html_fragment(row["id"], actor_user_id=user_id))
                rebuilt = service.build_problem(
                    self.problem,
                    self.user,
                    source_kind="workspace",
                    output_kind="html",
                    language="english",
                )
                self.assertEqual(rebuilt["status"], "ok")
                self.assertEqual(
                    service.html_fragment(rebuilt["id"], actor_user_id=user_id),
                    fragment,
                )
                row = rebuilt

    def test_pdf_failure_returns_the_latex_error_as_plain_text(self) -> None:
        compile_log = (
            "This is pdfTeX.\n"
            "! Undefined control sequence.\n"
            "l.42 \\BrokenStatementMacro\n"
            "No pages of output.\n"
        )
        def failed_tex(spec: ExecSpec) -> ExecResult:
            assert spec.cwd is not None
            (spec.cwd / spec.command[-1]).with_suffix(".log").write_text(compile_log, encoding="utf-8")
            return ExecResult(backend="fixture", status="error", returncode=1, elapsed_ms=1)

        with patch.object(
            runtime.tex_sandbox_backend,
            "run",
            side_effect=failed_tex,
        ), bind_application(app):
            response = problem_statement_pdf_page(
                self.problem,
                self.user,
                source="workspace",
                language="english",
            )
        problem_id = int(runtime.workspace_service.problem_row(self.problem)["id"])
        persisted = db_fetch_one(
            "SELECT id,status FROM statement_previews WHERE problem_id=? AND output_kind='pdf'",
            [problem_id],
        )
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted["status"], "failed")
        latex_log = runtime.storage_layout.resolve_preview_root(persisted["id"]) / "logs/latex.log"
        self.assertEqual(latex_log.read_text(encoding="utf-8"), compile_log)
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
            )
            store.finish(preview_id, status="ok", summary={})

        first = store.cached_problem(
            problem_id,
            actor_user_id=first_user_id,
            source_kind="workspace",
            output_kind="html",
            language="english",
            input_identity="same-content",
        )
        second = store.cached_problem(
            problem_id,
            actor_user_id=second_user_id,
            source_kind="workspace",
            output_kind="html",
            language="english",
            input_identity="same-content",
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

    def test_note_sections_preserve_commands_guards_and_content(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="statement-html-notes-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, root, True)
        render_root = root / "render"
        render_root.mkdir()
        guard = (
            "\\ifdefined\\Note\n"
            "  \\ifx\\Note\\empty\n"
            "    \\subsection*{Notes}\n"
            "  \\else\n"
            "    \\Note\n"
            "  \\fi\n"
            "\\else\n"
            "  \\subsection*{Notes}\n"
            "\\fi\n"
        )
        cases = (
            (
                "commands",
                "\\Note\nA single note.\n\\Notes\nSeveral notes.\n",
                [("h3", "Note"), ("h3", "Notes")],
                ("A single note.", "Several notes."),
                True,
            ),
            (
                "guard-content", guard + "Actual note content.\n",
                [("h3", "Note")], ("Actual note content.",), True,
            ),
            ("guard-empty", guard, [], (), True),
            (
                "unknown-condition", "\\ifdefined\\Note\nConditional content.\n\\fi\n",
                [], (), False,
            ),
        )
        for name, source, headings, content, warning_free in cases:
            with self.subTest(case=name):
                (render_root / "problem.tex").write_text(
                    "\\begin{problem}{Notes}{standard input}{standard output}"
                    "{1 second}{256 megabytes}\n" + source + "\\end{problem}\n",
                    encoding="utf-8",
                )
                result = runtime.statement_html_renderer.render(
                    render_root, root / name, subject_token=f"html-preview-{name}",
                )
                self.assertEqual(
                    [(tag, text) for tag, _, text in _headings(result.fragment)],
                    [("h2", "Notes"), *headings],
                )
                for text in content:
                    self.assertIn(text, result.fragment)
                if warning_free:
                    self.assertEqual(result.warnings, ())


class TestStatementNumbering(unittest.TestCase):
    def test_contest_numbering_preserves_statement_title_attributes(self) -> None:
        for fragment, expected_title, expected_attributes in (
            ("<section><h2>绝对多数</h2></section>", "D. 绝对多数", {}),
            (
                '<div><h2 id="problem-title" class="localized">璀璨宝石</h2></div>',
                "D. 璀璨宝石",
                {"id": "problem-title", "class": "localized"},
            ),
        ):
            with self.subTest(fragment=fragment):
                numbered = number_statement_fragment(fragment, "D")
                headings = _headings(numbered)
                self.assertEqual(len(headings), 1)
                self.assertEqual(headings[0][0], "h2")
                self.assertEqual(headings[0][1], expected_attributes)
                self.assertEqual(headings[0][2], expected_title)

    def test_contest_numbering_requires_a_statement_title(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing its title heading"):
            number_statement_fragment("<section><p>Body only.</p></section>", "A")
