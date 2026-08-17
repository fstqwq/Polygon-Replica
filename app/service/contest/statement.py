import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.config import ConfigValues
from app.main_util import problem_slug_leaf
from app.service.contest.naming import problem_source_folder
from app.service.contest.model import ContestBuildItemRecord
from app.service.contest.service import ContestService
from app.service.problem.runtime_config import problem_config_limits
from app.service.problem_package.service import VerifiedRevisionReader
from app.service.problem_package.statement_samples import (
    hydrate_verified_statement_samples,
)
from app.service.sandbox.base import ExecResult
from app.service.statement.constant import DEFAULT_OLYMP_STY
from app.service.statement.context import normalize_statement_language
from app.service.statement.render import (
    render_statement_problem_assets_for_language,
    statement_title_from_snapshot,
)
from app.service.statement.tex_compile import TexCompileService


_EXTRACTBB_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".pdf", ".png"}
_LATEX_JOB_NAME = "statements"
_LATEX_WRAPPER_NAME = "__contest_wrapper__.tex"
_BLANK_PAGES_MARKER = r"%\intentionallyblankpagestrue"
_BLANK_PAGES_ENABLED = _BLANK_PAGES_MARKER.removeprefix("%")
_CJK_ITALIC_OPTIONS = (
    r"[ItalicFont={[FandolKai-Regular.otf]},"
    r"BoldItalicFont={[FandolKai-Regular.otf]}]"
)
_CJK_MAIN_FONT_LINE = (
    rf"\setCJKmainfont{{Noto Serif CJK SC}}{_CJK_ITALIC_OPTIONS}"
)
_CJK_SANS_FONT_LINE = (
    rf"\setCJKsansfont{{Noto Sans CJK SC}}{_CJK_ITALIC_OPTIONS}"
)
_CJK_PREAMBLE_LINES = [
    r"% --- Engine-adaptive font loading ---",
    r"\usepackage{fontspec}",
    r"\usepackage{xeCJK}",
    _CJK_MAIN_FONT_LINE,
    _CJK_SANS_FONT_LINE,
    r"\setCJKmonofont{Noto Sans CJK SC}",
]
_TIKZ_LIBRARY_LINE_RE = re.compile(
    r"(?m)^[ \t]*\\usetikzlibrary\s*(?:\[[^\]\n]*\])?\s*\{([^}\n]+)\}"
    r"[ \t]*%?[ \t]*(?:\r?\n)?"
)
_COLOR_DEFINITION_LINE_RE = re.compile(
    r"(?m)^[ \t]*\\(?:definecolor|providecolor|colorlet)\s*"
    r"(?:\[[^\]\n]*\])?\{[^}\n]+\}(?:\s*\{[^}\n]*\}){1,2}"
    r"[ \t]*%?[ \t]*(?:\r?\n)?"
)


class ContestStatementService:
    """Build Contest PDFs from frozen verified revisions and durable sources."""

    def __init__(
        self,
        contest_service: ContestService,
        tex_compile_service: TexCompileService,
        config_values: ConfigValues,
    ) -> None:
        self._contest = contest_service
        self._tex = tex_compile_service
        self._config = config_values

    def languages(self, contest_id: int) -> list[str]:
        languages: list[str] = []
        seen: set[str] = set()
        for row in self._contest.statement_attachment_rows(contest_id):
            parts = Path(str(row["rel_path"])).parts
            if len(parts) < 3 or parts[0] != "statements":
                continue
            if parts[2] != "statements.tex":
                continue
            language = normalize_statement_language(parts[1])
            if language and language not in seen:
                seen.add(language)
                languages.append(language)
        return languages

    def resolve_language(self, contest_id: int, requested: str = "") -> str:
        explicit = normalize_statement_language(requested)
        if explicit:
            return explicit
        configured = normalize_statement_language(
            self._contest.statement_default_language(contest_id)
        )
        if configured:
            return configured
        languages = self.languages(contest_id)
        if "english" in languages:
            return "english"
        return languages[0] if languages else "english"

    @staticmethod
    def _latex_escape(value: object) -> str:
        replacements = {
            "\\": r"\textbackslash{}",
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "{": r"\{",
            "}": r"\}",
            "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}",
        }
        return "".join(replacements.get(character, character) for character in str(value))

    def default_statements_tex(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        language: str,
        problem_entries: Sequence[Mapping[str, object]],
        source_folder_map: dict[int, str],
    ) -> str:
        contest = self._contest.contest_context(contest_slug)
        if contest is None:
            raise ValueError("contest not found")
        properties = self._contest.overview_properties_map(contest_id, contest_slug)
        lines = [
            r"\documentclass[11pt,a4paper,oneside]{article}",
            *_CJK_PREAMBLE_LINES,
            r"\usepackage{amsmath}",
            r"\usepackage{amssymb}",
            r"\usepackage{olymp}",
            r"\usepackage{comment}",
            r"\usepackage{epigraph}",
            r"\usepackage{expdlist}",
            r"\usepackage{import}",
            r"\usepackage{graphicx}",
            r"\usepackage{tikz}",
            r"\usepackage{pgfplots}",
            r"\usepackage{multirow}",
            r"\usepackage{siunitx}",
            r"\usepackage[normalem]{ulem}",
            r"\usepackage{xparse}",
            r"\usepackage{wrapfig}",
            r"\usepackage{algorithm}",
            r"\usepackage{algpseudocode}",
            _BLANK_PAGES_MARKER,
            r"\begin{document}",
            r"\contest",
            "{" + self._latex_escape(contest["title"]) + "}%",
            "{" + self._latex_escape(properties.get("location", "")) + "}%",
            "{" + self._latex_escape(properties.get("date", "")) + "}%",
            r"\binoppenalty=10000",
            r"\relpenalty=10000",
            "",
        ]
        for entry in problem_entries:
            source_folder = problem_source_folder(entry, source_folder_map)
            import_path = f"../../problems/{source_folder}/statements/{language}/"
            lines.extend(
                [
                    r"\clearpage",
                    r"\graphicspath{{" + import_path + r"}}",
                    r"\def\ProblemIndex{"
                    + self._latex_escape(entry.get("idx") or "")
                    + "}",
                    r"\import{" + import_path + r"}{./problem.tex}",
                    "",
                ]
            )
        lines.append(r"\end{document}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _compile_target(root: Path, relative: Path) -> Path:
        safe_root = root.resolve()
        target = (root / relative).resolve()
        if safe_root not in target.parents:
            raise RuntimeError("invalid contest compile path")
        return target

    @staticmethod
    def _latex_uses_package(text: str, package: str) -> bool:
        pattern = re.compile(
            r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
        )
        return any(
            package in [name.strip() for name in match.group(1).split(",")]
            for match in pattern.finditer(text)
        )

    def _ensure_cjk_support(self, statements_tex: Path) -> None:
        text = statements_tex.read_text(encoding="utf-8", errors="replace")
        lines: list[str] = []
        if not self._latex_uses_package(text, "fontspec"):
            lines.append(r"\usepackage{fontspec}")
        if not self._latex_uses_package(text, "xeCJK"):
            lines.append(r"\usepackage{xeCJK}")
        if r"\setCJKmainfont" not in text:
            lines.append(_CJK_MAIN_FONT_LINE)
        if r"\setCJKsansfont" not in text:
            lines.append(_CJK_SANS_FONT_LINE)
        if r"\setCJKmonofont" not in text:
            lines.append(r"\setCJKmonofont{Noto Sans CJK SC}")
        if not lines:
            return
        block = "% --- CJK support for XeLaTeX contest compilation ---\n"
        block += "\n".join(lines) + "\n"
        match = re.search(
            r"\\documentclass\s*(?:\[[^\]\n]*\])?\s*\{[^}\n]+\}\s*",
            text,
        )
        if match is None:
            statements_tex.write_text(block + text, encoding="utf-8")
            return
        index = match.end()
        statements_tex.write_text(
            text[:index] + "\n" + block + text[index:],
            encoding="utf-8",
        )

    @staticmethod
    def _extract_color_definitions(path: Path) -> list[str]:
        if path.is_symlink() or not path.is_file():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        return list(
            dict.fromkeys(
                line
                for match in _COLOR_DEFINITION_LINE_RE.finditer(text)
                if (line := match.group(0).strip())
            )
        )

    @staticmethod
    def _hoist_tikz_libraries(problem_tex: Path) -> list[str]:
        if problem_tex.is_symlink() or not problem_tex.is_file():
            return []
        text = problem_tex.read_text(encoding="utf-8", errors="replace")
        libraries: list[str] = []

        def remove(match: re.Match[str]) -> str:
            for raw_name in match.group(1).split(","):
                name = raw_name.strip()
                if name and name not in libraries:
                    libraries.append(name)
            return ""

        updated = _TIKZ_LIBRARY_LINE_RE.sub(remove, text)
        if updated != text:
            problem_tex.write_text(updated, encoding="utf-8")
        return libraries

    @staticmethod
    def _insert_preamble_lines(statements_tex: Path, lines: list[str]) -> None:
        if not lines:
            return
        text = statements_tex.read_text(encoding="utf-8", errors="replace")
        begin = re.search(r"\\begin\s*\{document\}", text)
        preamble = text[: begin.start()] if begin is not None else text
        missing = [line for line in lines if line not in preamble]
        if not missing:
            return
        block = "% --- Hoisted from problem statement bodies ---\n"
        block += "\n".join(missing) + "\n"
        if begin is None:
            statements_tex.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
            return
        index = begin.start()
        statements_tex.write_text(
            text[:index] + block + text[index:],
            encoding="utf-8",
        )

    def _hoist_problem_preamble(
        self,
        *,
        compile_root: Path,
        statements_root: Path,
        language: str,
        results: list[dict[str, object]],
    ) -> None:
        tikz_libraries: list[str] = []
        color_definitions: list[str] = []
        for row in results:
            if row["status"] != "success":
                continue
            problem_tex = self._compile_target(
                compile_root,
                Path("problems")
                / str(row["source_folder"])
                / "statements"
                / language
                / "problem.tex",
            )
            for library in self._hoist_tikz_libraries(problem_tex):
                if library not in tikz_libraries:
                    tikz_libraries.append(library)
            preamble_lines = row.get("preamble_lines")
            if not isinstance(preamble_lines, list):
                preamble_lines = []
            for raw_line in preamble_lines:
                line = str(raw_line).strip()
                if line and line not in color_definitions:
                    color_definitions.append(line)
            for line in self._extract_color_definitions(problem_tex):
                if line not in color_definitions:
                    color_definitions.append(line)
        if not tikz_libraries and not color_definitions:
            return
        statements_tex = statements_root / "statements.tex"
        text = statements_tex.read_text(encoding="utf-8", errors="replace")
        lines: list[str] = []
        if color_definitions and not (
            self._latex_uses_package(text, "xcolor")
            or self._latex_uses_package(text, "color")
        ):
            lines.append(r"\usepackage{xcolor}")
        lines.extend(color_definitions)
        if tikz_libraries and not self._latex_uses_package(text, "tikz"):
            lines.append(r"\usepackage{tikz}")
        if tikz_libraries:
            lines.append(r"\usetikzlibrary{" + ",".join(tikz_libraries) + "}")
        self._insert_preamble_lines(statements_tex, lines)

    @staticmethod
    def _log_tail(statements_root: Path) -> str:
        log_path = statements_root / f"{_LATEX_JOB_NAME}.log"
        try:
            if log_path.is_symlink() or not log_path.is_file():
                return ""
            return log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        except OSError:
            return ""

    @staticmethod
    def _compile_error(output: str, returncode: int | None) -> str:
        lowered = output.lower()
        for format_name in ("xelatex.fmt", "latex.fmt", "mpost.fmt"):
            if "can't find the format file" in lowered and format_name in lowered:
                return f"missing LaTeX format {format_name}"
        if "dvipdfmx:fatal" in lowered:
            return "dvipdfmx failed"
        missing = re.search("File `([^`]+\\.sty)' not found", output)
        if missing is not None:
            return f"missing LaTeX package {missing.group(1).strip()}"
        package_error = re.search(
            r"^!\s*(?:Package\s+)?([^:\n]+?)\s+Error:\s*(.+)$",
            output,
            flags=re.MULTILINE,
        )
        if package_error is not None:
            return f"{package_error.group(1).strip()} Error: {package_error.group(2).strip()}"
        bang_error = re.search(r"^!\s*(.+)$", output, flags=re.MULTILINE)
        if bang_error is not None:
            return bang_error.group(1).strip()
        return "latex compile failed" if int(returncode or 0) != 0 else ""

    @staticmethod
    def _append_log(log_path: Path, *, title: str, result: ExecResult) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"== {title} ==\n")
            stream.write(f"status: {result.status}\n")
            stream.write(f"returncode: {result.returncode}\n")
            stream.write(f"elapsed_ms: {result.elapsed_ms}\n")
            stream.write("[stdout]\n")
            stream.write(result.stdout or "")
            stream.write("\n[stderr]\n")
            stream.write(result.stderr or "")
            stream.write("\n\n")

    def _run_tex(
        self,
        command: list[str],
        cwd: Path,
        log_path: Path,
        *,
        title: str,
        extra_mounts: tuple[Path, ...],
        env: dict[str, str],
    ) -> ExecResult:
        merged_env = dict(os.environ)
        merged_env.update(env)
        result = self._tex.run(
            command=command,
            cwd=cwd,
            extra_mounts=extra_mounts,
            env=merged_env,
        )
        self._append_log(log_path, title=title, result=result)
        return result

    def _prepare_bounding_boxes(
        self,
        compile_root: Path,
        log_path: Path,
        *,
        mounts: tuple[Path, ...],
        env: dict[str, str],
    ) -> str:
        safe_root = compile_root.resolve()
        for file_path in sorted(compile_root.rglob("*")):
            if file_path.is_symlink() or not file_path.is_file():
                continue
            if file_path.suffix.lower() not in _EXTRACTBB_SUFFIXES:
                continue
            if safe_root not in file_path.resolve().parents:
                raise RuntimeError("invalid contest compile asset path")
            relative_name = file_path.relative_to(compile_root).as_posix()
            result = self._run_tex(
                ["extractbb", file_path.name],
                file_path.parent,
                log_path,
                title=f"extractbb {relative_name}",
                extra_mounts=mounts,
                env=env,
            )
            if result.timed_out:
                return f"extractbb timeout for {relative_name}"
            if int(result.returncode or 0) != 0:
                return f"extractbb failed for {relative_name}"
        return ""

    def _tex_env(self, compile_root: Path) -> dict[str, str]:
        paths = {
            "TEXMFVAR": ".texmf-var",
            "TEXMFCACHE": ".texmf-cache",
            "TEXMFCONFIG": ".texmf-config",
            "VARTEXFONTS": ".texfonts",
            "TEXMFOUTPUT": ".texmf-output",
        }
        result = {"HOME": str(compile_root)}
        for name, part in paths.items():
            path = self._compile_target(compile_root, Path(part))
            path.mkdir(parents=True, exist_ok=True)
            result[name] = str(path)
        return result

    def _copy_language_tree(
        self,
        *,
        source_snapshot: Path,
        language: str,
        compile_root: Path,
    ) -> Path:
        language_root = source_snapshot / "statements" / language
        if language_root.exists():
            for source_path in language_root.rglob("*"):
                if source_path.is_symlink():
                    raise RuntimeError(
                        f"contest statement source is a symlink: {source_path}"
                    )
                if source_path.is_dir():
                    continue
                if not source_path.is_file():
                    raise RuntimeError(
                        f"contest statement source is not regular: {source_path}"
                    )
                relative = source_path.relative_to(source_snapshot)
                target = self._compile_target(compile_root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
        statements_root = self._compile_target(
            compile_root,
            Path("statements") / language,
        )
        statements_root.mkdir(parents=True, exist_ok=True)
        olymp_sty = statements_root / "olymp.sty"
        if olymp_sty.is_symlink() or not olymp_sty.exists():
            olymp_sty.write_text(DEFAULT_OLYMP_STY, encoding="utf-8")
        statements_tex = statements_root / "statements.tex"
        if statements_tex.is_symlink() or not statements_tex.is_file():
            raise RuntimeError(
                f"contest statements.tex missing for language: {language}"
            )
        self._ensure_cjk_support(statements_tex)
        return statements_root

    def _prepare_problem(
        self,
        *,
        compile_root: Path,
        entry: ContestBuildItemRecord,
        source_folder: str,
        language: str,
        reader: VerifiedRevisionReader,
    ) -> dict[str, object]:
        problem_slug = str(entry["problem_slug"])
        item: dict[str, object] = {
            "idx": str(entry["idx"]),
            "problem_id": int(entry["problem_id"]),
            "problem_slug": problem_slug,
            "source_folder": source_folder,
            "status": "failed",
            "source_commit": reader.manifest["source_commit"],
            "error": "",
        }
        if not source_folder:
            item["error"] = f"contest source folder missing for {problem_slug}"
            return item
        try:
            hydrate_verified_statement_samples(
                reader,
                tests_spec_max_bytes=self._config.integer("TEXTAREA_MAX_BYTES"),
                statement_sample_max_bytes=self._config.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
            )
            target = self._compile_target(
                compile_root,
                Path("problems") / source_folder / "statements" / language,
            )
            render_statement_problem_assets_for_language(
                reader.root,
                language,
                target,
                problem_title=statement_title_from_snapshot(
                    reader.root,
                    fallback_title=problem_slug_leaf(problem_slug),
                    language=language,
                ),
                tests_spec_max_bytes=self._config.integer("TEXTAREA_MAX_BYTES"),
                statement_sample_max_bytes=self._config.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
                problem_limits=problem_config_limits(self._config),
            )
            item["preamble_lines"] = self._extract_color_definitions(
                reader.root / "statement" / "olymp.sty"
            )
            item["verified_revision_id"] = str(entry["materialization_id"])
            item["status"] = "success"
        except Exception as exc:  # Each problem is represented in the build report.
            item["error"] = str(exc)
        return item

    @staticmethod
    def _latex_wrapper(statements_root: Path) -> Path:
        wrapper = (statements_root / _LATEX_WRAPPER_NAME).resolve()
        wrapper.write_text(
            "\\AtBeginDocument{%\n"
            "  \\providecommand{\\url}[1]{\\texttt{#1}}%\n"
            "  \\providecommand{\\href}[2]{#2}%\n"
            "}\n"
            "\\input{statements.tex}\n",
            encoding="utf-8",
        )
        return wrapper

    def build_pdf(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        job_id: str,
        language: str,
        insert_blank_pages: bool,
        readers: dict[str, VerifiedRevisionReader],
    ) -> dict[str, object]:
        job_root = self._contest.job_root(contest_slug, job_id)
        compile_root = (job_root / "contest-pdf-src").resolve()
        shutil.rmtree(compile_root, ignore_errors=True)
        compile_root.mkdir(parents=True, exist_ok=True)
        mounts = (compile_root,)
        env = self._tex_env(compile_root)
        log_path = job_root / "logs" / "contest-pdf.log"
        entries = self._contest.build_items(job_id)
        source_folders = {
            int(entry["problem_id"]): str(entry["statement_folder"])
            for entry in entries
            if entry["statement_folder"]
        }
        statements_root = self._copy_language_tree(
            source_snapshot=job_root / "contest-sources",
            language=language,
            compile_root=compile_root,
        )
        if insert_blank_pages:
            statements_tex = statements_root / "statements.tex"
            statements_tex.write_bytes(
                statements_tex.read_bytes().replace(
                    _BLANK_PAGES_MARKER.encode("ascii"),
                    _BLANK_PAGES_ENABLED.encode("ascii"),
                )
            )
        results = [
            self._prepare_problem(
                compile_root=compile_root,
                entry=entry,
                source_folder=problem_source_folder(entry, source_folders),
                language=language,
                reader=readers[str(entry["materialization_id"])],
            )
            for entry in entries
        ]
        failed = [row for row in results if row["status"] != "success"]
        summary: dict[str, object] = {
            "job_type": "pdf",
            "contest_slug": contest_slug,
            "language": language,
            "results": results,
            "totals": {
                "total": len(results),
                "success": len(results) - len(failed),
                "failed": len(failed),
            },
        }
        if failed:
            summary["error"] = str(failed[0]["error"] or "problem preparation failed")
            return summary
        self._hoist_problem_preamble(
            compile_root=compile_root,
            statements_root=statements_root,
            language=language,
            results=results,
        )
        for row in results:
            problem_root = self._compile_target(
                compile_root,
                Path("problems")
                / str(row["source_folder"])
                / "statements"
                / language,
            )
            for mp_file in sorted(problem_root.glob("*.mp")):
                result = self._run_tex(
                    ["mpost", mp_file.name],
                    problem_root,
                    log_path,
                    title=f"{row['problem_slug']} :: mpost {mp_file.name}",
                    extra_mounts=mounts,
                    env=env,
                )
                if result.timed_out or int(result.returncode or 0) != 0:
                    state = "timeout" if result.timed_out else "failed"
                    summary["error"] = (
                        f"mpost {state} for {row['problem_slug']}: {mp_file.name}"
                    )
                    return summary
        bounding_error = self._prepare_bounding_boxes(
            compile_root,
            log_path,
            mounts=mounts,
            env=env,
        )
        if bounding_error:
            summary["error"] = bounding_error
            return summary
        wrapper = self._latex_wrapper(statements_root)
        for pass_number in (1, 2):
            title = f"xelatex pass {pass_number}"
            result = self._run_tex(
                [
                    "xelatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"-jobname={_LATEX_JOB_NAME}",
                    wrapper.name,
                ],
                statements_root,
                log_path,
                title=title,
                extra_mounts=mounts,
                env=env,
            )
            output = "\n".join(
                part
                for part in (
                    result.stdout,
                    result.stderr,
                    self._log_tail(statements_root),
                )
                if part
            ).strip()
            if result.timed_out:
                summary["error"] = f"{title} timeout"
                return summary
            if int(result.returncode or 0) != 0:
                summary["error"] = self._compile_error(output, result.returncode)
                return summary
        generated_pdf = statements_root / "statements.pdf"
        if generated_pdf.is_symlink() or not generated_pdf.is_file():
            summary["error"] = "contest pdf missing after compile"
            return summary
        pdf_dir = job_root / "contest-pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        target_pdf = (pdf_dir / "statements.pdf").resolve()
        shutil.copy2(generated_pdf, target_pdf)
        summary.update(
            {
                "artifact_id": "",
                "filename": f"{contest_slug}-{language}-statements.pdf",
                "_artifact_path": str(target_pdf),
                "_artifact_type": "contest-pdf",
                "_artifact_filename": f"{contest_slug}-{language}-statements.pdf",
            }
        )
        return summary
