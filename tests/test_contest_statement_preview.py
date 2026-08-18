import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast

from app.service.contest.service import ContestService
from app.service.contest.statement import ContestStatementService
from app.service.sandbox.base import ExecResult
from app.service.statement.tex_compile import TexCompileService

from tests.common import suite_root


class _RecordingTexCompiler:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self,
        *,
        command: list[str],
        cwd: Path,
        read_only_mounts: tuple[Path, ...] = (),
        writable_mounts: tuple[Path, ...] = (),
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del read_only_mounts, writable_mounts, env
        self.commands.append(list(command))
        if command[0] == "xelatex":
            (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n% complete contest\n")
            (cwd / "statements.log").write_text("complete\n", encoding="utf-8")
        return ExecResult(
            backend="fixture",
            status="ok",
            returncode=0,
            elapsed_ms=1,
        )


class TestContestStatementPreview(unittest.TestCase):
    def test_pdf_preview_compiles_one_complete_contest_document(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="contest-pdf-preview-", dir=suite_root()))
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "contest-source"
        statement_root = source / "statements" / "english"
        statement_root.mkdir(parents=True)
        statement_root.joinpath("statements.tex").write_text(
            "\\documentclass{article}\n"
            "\\usepackage{import}\n"
            "\\begin{document}\n"
            "\\import{../../problems/alpha/statements/english/}{problem.tex}\n"
            "\\import{../../problems/beta/statements/english/}{problem.tex}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
        render_roots: dict[int, Path] = {}
        for problem_id, folder, title in (
            (11, "alpha", "Problem A"),
            (12, "beta", "Problem B"),
        ):
            render_root = root / f"render-{problem_id}"
            render_root.mkdir()
            render_root.joinpath("problem.tex").write_text(
                f"\\section*{{{title}}}\n",
                encoding="utf-8",
            )
            render_root.joinpath("examples.tex").write_text("", encoding="utf-8")
            render_root.joinpath("olymp.sty").write_text("", encoding="utf-8")
            render_roots[problem_id] = render_root

        compiler = _RecordingTexCompiler()
        service = ContestStatementService(
            cast(ContestService, object()),
            cast(TexCompileService, compiler),
        )
        output = root / "preview"
        summary = service.build_preview_pdf(
            contest_slug="complete-contest",
            language="english",
            insert_blank_pages=False,
            source_snapshot=source,
            problem_entries=[
                {
                    "idx": "A",
                    "problem_id": 11,
                    "problem_slug": "owner/alpha",
                    "statement_folder": "alpha",
                },
                {
                    "idx": "B",
                    "problem_id": 12,
                    "problem_slug": "owner/beta",
                    "statement_folder": "beta",
                },
            ],
            render_roots=render_roots,
            output_root=output,
        )

        self.assertNotIn("error", summary)
        self.assertEqual(summary["pdf"], "pdf/statement.pdf")
        self.assertTrue((output / "pdf" / "statement.pdf").is_file())
        compile_root = output / "contest-pdf-src"
        self.assertTrue(
            (compile_root / "problems/alpha/statements/english/problem.tex").is_file()
        )
        self.assertTrue(
            (compile_root / "problems/beta/statements/english/problem.tex").is_file()
        )
        self.assertEqual(
            [command[0] for command in compiler.commands],
            ["xelatex", "xelatex"],
        )
        self.assertFalse(any(command[0] == "pdfunite" for command in compiler.commands))
