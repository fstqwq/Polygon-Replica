import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.service.contest.naming import problem_source_folder
from app.service.contest.property import contest_template_properties
from app.service.contest.service import (
    ContestProblem,
    ContestService,
)
from app.service.contest.statement_source_contract import (
    CONTEST_STATEMENT_OUTPUT_NAME,
    CONTEST_STATEMENT_SHARED_PROTECTED_FILES,
    CONTEST_STATEMENT_SHARED_SCOPE,
    CONTEST_STATEMENT_STYLE_NAME,
    CONTEST_STATEMENT_TEMPLATE_NAME,
)
from app.service.sandbox.base import ExecResult
from app.service.statement.constant import (
    DEFAULT_OLYMP_STY,
    DEFAULT_STATEMENT_TEMPLATE,
)
from app.service.statement.context import normalize_statement_language
from app.service.statement.ftl.renderer import render_ftl_template
from app.service.statement.latex_error import (
    latex_error_excerpt,
    latex_log_for_display,
)
from app.service.statement.tex_compile import TexCompileService


_EXTRACTBB_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".pdf", ".png"}
_LATEX_JOB_NAME = "statements"
_LATEX_WRAPPER_NAME = "__contest_wrapper__.tex"
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
    """Compile complete Contest documents into caller-owned output roots."""

    def __init__(
        self,
        contest_service: ContestService,
        tex_compile_service: TexCompileService,
        *,
        error_text_limit_bytes: int,
    ) -> None:
        self._contest = contest_service
        self._tex = tex_compile_service
        self._error_text_limit_bytes = max(1, error_text_limit_bytes)

    def languages(self, contest_id: int) -> list[str]:
        languages: list[str] = []
        seen: set[str] = set()
        for row in self._contest.statement_attachment_rows(contest_id):
            parts = Path(str(row["rel_path"])).parts
            if len(parts) < 3 or parts[0] != "statements":
                continue
            if parts[1] == CONTEST_STATEMENT_SHARED_SCOPE:
                continue
            if parts[2] != CONTEST_STATEMENT_TEMPLATE_NAME:
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
        languages = self.languages(contest_id)
        if "english" in languages:
            return "english"
        return languages[0] if languages else ""

    @staticmethod
    def default_statements_template() -> str:
        """Return the canonical FTL source used for Contest statements."""

        return DEFAULT_STATEMENT_TEMPLATE

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
        return "".join(
            replacements.get(character, character)
            for character in str(value)
        )

    def _statements_template_context(
        self,
        *,
        contest_slug: str,
        language: str,
        problem_entries: Sequence[ContestProblem],
        source_folder_map: dict[int, str],
    ) -> dict[str, object]:
        contest = self._contest.contest_context(contest_slug)
        if contest is None:
            raise ValueError("contest not found")
        localized_properties = self._contest.localized_overview_properties_map(
            int(contest["id"]),
            contest_slug,
            language,
        )
        properties = contest_template_properties(localized_properties)
        for key in ("title", "location", "date"):
            properties[key] = self._latex_escape(localized_properties.get(key, ""))
        statements: list[dict[str, str]] = []
        for entry in problem_entries:
            source_folder = problem_source_folder(entry, source_folder_map)
            statements.append(
                {
                    "path": (
                        f"../../problems/{source_folder}/statements/{language}/"
                    ),
                    "index": self._latex_escape(entry.get("idx") or ""),
                    "file": "problem.tex",
                }
            )
        context: dict[str, object] = dict(properties)
        context.update({
            "properties": dict(properties),
            "contest": {
                "name": properties["title"],
                "location": properties["location"],
                "date": properties["date"],
                "language": language,
            },
            "language": language,
            "shortProblemTitle": True,
            "providedStatementsCommands": [],
            "statements": statements,
        })
        return context

    def _render_statements_template(
        self,
        statements_root: Path,
        *,
        contest_slug: str,
        language: str,
        problem_entries: Sequence[ContestProblem],
        source_folder_map: dict[int, str],
    ) -> None:
        statements_ftl = statements_root / CONTEST_STATEMENT_TEMPLATE_NAME
        statements_tex = statements_root / CONTEST_STATEMENT_OUTPUT_NAME
        template_text = statements_ftl.read_text(encoding="utf-8")
        rendered = render_ftl_template(
            template_text,
            self._statements_template_context(
                contest_slug=contest_slug,
                language=language,
                problem_entries=problem_entries,
                source_folder_map=source_folder_map,
            ),
        )
        statements_tex.write_text(rendered, encoding="utf-8", newline="\n")

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
        statements_tex = statements_root / CONTEST_STATEMENT_OUTPUT_NAME
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
    def _latex_log_text(statements_root: Path) -> str:
        log_path = statements_root / f"{_LATEX_JOB_NAME}.log"
        try:
            if log_path.is_symlink() or not log_path.is_file():
                return ""
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

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
        writable_mounts: tuple[Path, ...],
        env: dict[str, str],
    ) -> ExecResult:
        merged_env = dict(os.environ)
        merged_env.update(env)
        result = self._tex.run(
            command=command,
            cwd=cwd,
            writable_mounts=writable_mounts,
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
                writable_mounts=mounts,
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
        statements_root = self._compile_target(
            compile_root,
            Path("statements") / language,
        )
        statements_root.mkdir(parents=True, exist_ok=True)
        shared_root = source_snapshot / "statements" / CONTEST_STATEMENT_SHARED_SCOPE
        language_root = source_snapshot / "statements" / language

        for protected_name in CONTEST_STATEMENT_SHARED_PROTECTED_FILES:
            protected_path = shared_root / protected_name
            if protected_path.exists() or protected_path.is_symlink():
                raise RuntimeError(
                    "shared Contest statement files cannot replace "
                    f"{protected_name}"
                )

        for source_root in (shared_root, language_root):
            if not source_root.exists():
                continue
            if source_root.is_symlink() or not source_root.is_dir():
                raise RuntimeError(
                    f"contest statement source is not a directory: {source_root}"
                )
            for source_path in source_root.rglob("*"):
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
                relative = source_path.relative_to(source_root)
                target = self._compile_target(statements_root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
        olymp_sty = statements_root / CONTEST_STATEMENT_STYLE_NAME
        if olymp_sty.is_symlink() or not olymp_sty.exists():
            olymp_sty.write_text(DEFAULT_OLYMP_STY, encoding="utf-8")
        statements_ftl = statements_root / CONTEST_STATEMENT_TEMPLATE_NAME
        if statements_ftl.is_symlink() or not statements_ftl.is_file():
            raise RuntimeError(
                "contest statement template missing for language "
                f"{language}: {CONTEST_STATEMENT_TEMPLATE_NAME}"
            )
        return statements_root

    @staticmethod
    def _latex_wrapper(statements_root: Path) -> Path:
        wrapper = (statements_root / _LATEX_WRAPPER_NAME).resolve()
        wrapper.write_text(
            "\\AtBeginDocument{%\n"
            "  \\providecommand{\\url}[1]{\\texttt{#1}}%\n"
            "  \\providecommand{\\href}[2]{#2}%\n"
            "}\n"
            f"\\input{{{CONTEST_STATEMENT_OUTPUT_NAME}}}\n",
            encoding="utf-8",
        )
        return wrapper

    def build_preview_pdf(
        self,
        *,
        contest_slug: str,
        language: str,
        source_snapshot: Path,
        problem_entries: Sequence[ContestProblem],
        render_roots: Mapping[int, Path],
        output_root: Path,
    ) -> dict[str, object]:
        """Compile one complete Contest document into a Preview-owned root."""

        compile_root = (output_root / "contest-pdf-src").resolve()
        shutil.rmtree(compile_root, ignore_errors=True)
        compile_root.mkdir(parents=True, exist_ok=True)
        mounts = (compile_root,)
        env = self._tex_env(compile_root)
        log_path = output_root / "logs" / "contest-pdf.log"
        source_folders = {
            int(entry["problem_id"]): str(entry.get("statement_folder") or "")
            for entry in problem_entries
            if entry.get("statement_folder")
        }
        statements_root = self._copy_language_tree(
            source_snapshot=source_snapshot,
            language=language,
            compile_root=compile_root,
        )
        self._render_statements_template(
            statements_root,
            contest_slug=contest_slug,
            language=language,
            problem_entries=problem_entries,
            source_folder_map=source_folders,
        )
        self._ensure_cjk_support(statements_root / CONTEST_STATEMENT_OUTPUT_NAME)
        results: list[dict[str, object]] = []
        for entry in problem_entries:
            problem_id = int(entry["problem_id"])
            problem_slug = str(entry["problem_slug"])
            source_folder = problem_source_folder(entry, source_folders)
            item: dict[str, object] = {
                "idx": str(entry.get("idx") or ""),
                "problem_id": problem_id,
                "problem_slug": problem_slug,
                "source_folder": source_folder,
                "status": "failed",
                "error": "",
            }
            source_root = render_roots.get(problem_id)
            if not source_folder:
                item["error"] = f"contest source folder missing for {problem_slug}"
            elif source_root is None:
                item["error"] = f"statement render tree missing for {problem_slug}"
            else:
                try:
                    target = self._compile_target(
                        compile_root,
                        Path("problems")
                        / source_folder
                        / "statements"
                        / language,
                    )
                    shutil.copytree(source_root, target)
                    item["preamble_lines"] = self._extract_color_definitions(
                        source_root / "olymp.sty"
                    )
                    item["status"] = "success"
                except Exception as exc:
                    item["error"] = str(exc)
            results.append(item)
        failed = [row for row in results if row["status"] != "success"]
        summary: dict[str, object] = {
            "job_type": "preview-pdf",
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
                    writable_mounts=mounts,
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
                writable_mounts=mounts,
                env=env,
            )
            latex_log = self._latex_log_text(statements_root)
            command_output = "\n".join(
                part
                for part in (
                    result.stdout,
                    result.stderr,
                )
                if part
            ).strip()
            error_source = latex_log or command_output
            display_log = latex_log_for_display(
                error_source,
                path_prefixes=[(str(compile_root), ".")],
            )
            latex_log_path = output_root / "logs" / "latex.log"
            latex_log_path.parent.mkdir(parents=True, exist_ok=True)
            latex_log_path.write_text(display_log, encoding="utf-8")
            summary["latex_log"] = "logs/latex.log"
            if result.timed_out:
                summary["error"] = latex_error_excerpt(
                    error_source,
                    max_bytes=self._error_text_limit_bytes,
                    path_prefixes=[(str(compile_root), ".")],
                ) or f"{title} timeout"
                return summary
            if int(result.returncode or 0) != 0:
                summary["error"] = latex_error_excerpt(
                    error_source,
                    max_bytes=self._error_text_limit_bytes,
                    path_prefixes=[(str(compile_root), ".")],
                ) or "LaTeX compilation failed."
                return summary
        generated_pdf = statements_root / "statements.pdf"
        if generated_pdf.is_symlink() or not generated_pdf.is_file():
            summary["error"] = "contest pdf missing after compile"
            return summary
        target_pdf = output_root / "pdf" / "statement.pdf"
        target_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_pdf, target_pdf)
        summary.update(
            {
                "pdf": "pdf/statement.pdf",
                "filename": f"{contest_slug}-{language}-statements.pdf",
                "log": "logs/contest-pdf.log",
            }
        )
        return summary
