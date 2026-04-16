from __future__ import annotations

import re
from pathlib import Path


STATEMENT_DIR = Path("statement")
STATEMENT_TEMPLATE_REL = STATEMENT_DIR / "statements.ftl"
STATEMENT_PROBLEM_REL = STATEMENT_DIR / "problem.tex"
STATEMENT_STYLE_REL = STATEMENT_DIR / "olymp.sty"
STATEMENT_MAIN_REL = STATEMENT_DIR / "main.tex"
STATEMENT_RENDERED_DIR_REL = STATEMENT_DIR / "rendered"
STATEMENT_SECTIONS_DIR = Path("statement-sections")
STATEMENT_ASSETS_DIR = Path("statement-assets")
STATEMENT_CANONICAL_SECTION_FILES = frozenset(
    {
        "name.tex",
        "legend.tex",
        "input.tex",
        "output.tex",
        "interaction.tex",
        "scoring.tex",
        "notes.tex",
    }
)
TESTS_ANSWERS_DIR_REL = Path("tests/answers")
WF_STYLE_DIR = Path("third_party") / "Polygon-WF-Styles"
WF_STYLE_STATEMENTS_REL = WF_STYLE_DIR / "statements.ftl"
WF_STYLE_OLYMP_REL = WF_STYLE_DIR / "olymp.sty"
DEFAULT_PROBLEM_TITLE = "Sample Problem"
FTL_COMMENT_RE = re.compile(r"<#--.*?-->", re.DOTALL)
FTL_LIST_RE = re.compile(r"^list\s+(.+?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", re.DOTALL)
STANDALONE_OPEN_DIRECTIVE_PREFIXES = ("if ", "elseif ", "list ", "assign ")
STANDALONE_OPEN_DIRECTIVE_EXACT = {"else"}
STANDALONE_CLOSE_DIRECTIVES = {"if", "list"}

DEFAULT_STATEMENT_PROBLEM_TEMPLATE = r"""\begin{problem}{${problem.name}}{${problem.inputFile}}{${problem.outputFile}}{${(problem.timeLimit/1000)?c} seconds}{${(problem.memoryLimit/1048576)?c} megabytes}
${problem.legend}
<#if problem.input?? && (problem.input?length > 0)>
\InputFile
${problem.input}
</#if>
<#if problem.output?? && (problem.output?length > 0)>
\OutputFile
${problem.output}
</#if>
<#if problem.interaction?? && (problem.interaction?length > 0)>
\Interaction
${problem.interaction}
</#if>
<#if problem.scoring?? && (problem.scoring?length > 0)>
\Scoring
${problem.scoring}
</#if>
<#if (problem.sampleTests?size>0)>
\Example<#if (problem.sampleTests?size>1)>s</#if>
\begin{example}
<#list problem.sampleTests as test>
\exmpfile{${test.inputFile}}{${test.outputFile}}%
</#list>
\end{example}
</#if>
<#if (problem.notes??) && (problem.notes?length > 0)>
\ifdefined\Note
  \ifx\Note\empty
    \subsection*{Notes}
  \else
    \Note
  \fi
\else
  \subsection*{Notes}
\fi
${problem.notes}
</#if>
\end{problem}
"""

STATEMENT_RENDERER_SIGNATURE_VERSION = "2026-03-02-short-problem-title-tex-pass2-tests-sample-source"


def _read_required_text(path: Path, *, label: str, allow_empty: bool = False) -> str:
    readable = path.as_posix()
    if path.is_symlink():
        raise RuntimeError(f"{label} must be a regular file: {readable}")
    if not path.exists():
        raise RuntimeError(f"{label} is missing: {readable}")
    if not path.is_file():
        raise RuntimeError(f"{label} is not a file: {readable}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} must be valid UTF-8: {readable}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to read {label}: {readable}: {exc}") from exc
    if (not allow_empty) and (not str(text).strip()):
        raise RuntimeError(f"{label} is empty: {readable}")
    return text


def _load_repo_required_text(rel_path: Path, *, label: str) -> str:
    root = Path(__file__).resolve().parents[3]
    return _read_required_text(root / rel_path, label=label)


DEFAULT_STATEMENT_TEMPLATE = _load_repo_required_text(
    WF_STYLE_STATEMENTS_REL,
    label=f"canonical statement template ({WF_STYLE_STATEMENTS_REL.as_posix()})",
)
DEFAULT_OLYMP_STY = _load_repo_required_text(
    WF_STYLE_OLYMP_REL,
    label=f"canonical olymp style ({WF_STYLE_OLYMP_REL.as_posix()})",
)


def is_canonical_statement_section_entry(rel_path: str | Path) -> bool:
    rel = Path(rel_path)
    return len(rel.parts) == 1 and rel.name in STATEMENT_CANONICAL_SECTION_FILES


