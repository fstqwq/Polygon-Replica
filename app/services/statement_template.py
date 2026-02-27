from __future__ import annotations

import hashlib
import re
from pathlib import Path


STATEMENT_DIR = Path("statement")
STATEMENT_TEMPLATE_REL = STATEMENT_DIR / "template.tex"
STATEMENT_CONTENT_REL = STATEMENT_DIR / "content.tex"
STATEMENT_STYLE_REL = STATEMENT_DIR / "olmpy.sty"
STATEMENT_MAIN_REL = STATEMENT_DIR / "main.tex"
STATEMENT_CONTENT_TOKEN = "%%__POLYGON_REPLICA_CONTENT__%%"
STATEMENT_TITLE_TOKEN = "%%__POLYGON_REPLICA_TITLE__%%"
DEFAULT_PROBLEM_TITLE = "Sample Problem"
PROBLEM_TITLE_CMD_RE = re.compile(r"\\ProblemTitle\s*\{.*?\}", re.DOTALL)
BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}")

DEFAULT_STATEMENT_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage{olmpy}

\begin{document}

\ProblemTitle{%%__POLYGON_REPLICA_TITLE__%%}

%%__POLYGON_REPLICA_CONTENT__%%

\end{document}
"""

DEFAULT_STATEMENT_CONTENT = r"""\Section{Problem}
Write your statement body here.

\Section{Input}
Describe the input format.

\Section{Output}
Describe the output format.

\Section{Examples}
\begin{verbatim}
Input
1

Output
1
\end{verbatim}
"""

DEFAULT_OLMPY_STY = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{olmpy}[2026/02/23 Polygon-Replica statement helpers]

\RequirePackage[utf8]{inputenc}
\RequirePackage[T1]{fontenc}
\RequirePackage{lmodern}
\RequirePackage{geometry}
\RequirePackage{xcolor}
\RequirePackage{amsmath,amssymb}

\geometry{margin=1in}

\newcommand{\ProblemTitle}[1]{%
  \begin{center}
    {\Large\bfseries #1}
  \end{center}
  \vspace{0.6em}
}

\newcommand{\Section}[1]{%
  \vspace{0.7em}
  \noindent{\bfseries #1}\par
  \vspace{0.25em}
}
"""

SIGNATURE_CHUNK_SIZE = 65536


def _safe_read_text(path: Path, fallback: str) -> str:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return fallback


def _escape_tex_text(value: str) -> str:
    text = str(value or "")
    escaped: list[str] = []
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\^{}",
        "~": r"\~{}",
    }
    for ch in text:
        escaped.append(replacements.get(ch, ch))
    return "".join(escaped)


def _apply_problem_title(text: str, problem_title: str | None) -> str:
    raw_title = str(problem_title or "").strip() or DEFAULT_PROBLEM_TITLE
    title_tex = _escape_tex_text(raw_title)
    if STATEMENT_TITLE_TOKEN in text:
        return text.replace(STATEMENT_TITLE_TOKEN, title_tex)
    replacement = f"\\ProblemTitle{{{title_tex}}}"
    if PROBLEM_TITLE_CMD_RE.search(text):
        # Use a callable replacement so backslashes in LaTeX are kept literally.
        return PROBLEM_TITLE_CMD_RE.sub(lambda _m: replacement, text, count=1)
    if BEGIN_DOCUMENT_RE.search(text):
        return BEGIN_DOCUMENT_RE.sub(lambda m: f"{m.group(0)}\n\n{replacement}", text, count=1)
    return f"{replacement}\n\n{text}"


def seed_statement_sources(workspace: Path) -> None:
    statement_root = workspace / STATEMENT_DIR
    statement_root.mkdir(parents=True, exist_ok=True)
    template_path = workspace / STATEMENT_TEMPLATE_REL
    content_path = workspace / STATEMENT_CONTENT_REL
    style_path = workspace / STATEMENT_STYLE_REL
    main_path = workspace / STATEMENT_MAIN_REL

    if not template_path.exists():
        template_path.write_text(DEFAULT_STATEMENT_TEMPLATE, encoding="utf-8")
    if not content_path.exists():
        content_path.write_text(DEFAULT_STATEMENT_CONTENT, encoding="utf-8")
    if not style_path.exists():
        style_path.write_text(DEFAULT_OLMPY_STY, encoding="utf-8")
    if not main_path.exists():
        seeded = DEFAULT_STATEMENT_TEMPLATE.replace(STATEMENT_CONTENT_TOKEN, DEFAULT_STATEMENT_CONTENT)
        seeded = _apply_problem_title(seeded, DEFAULT_PROBLEM_TITLE)
        main_path.write_text(
            seeded,
            encoding="utf-8",
        )


def render_statement_main(statement_root: Path, problem_title: str | None = None) -> Path:
    statement_root.mkdir(parents=True, exist_ok=True)
    template_path = statement_root / STATEMENT_TEMPLATE_REL.name
    content_path = statement_root / STATEMENT_CONTENT_REL.name
    style_path = statement_root / STATEMENT_STYLE_REL.name
    main_path = statement_root / STATEMENT_MAIN_REL.name

    template_exists = template_path.exists() and template_path.is_file() and not template_path.is_symlink()
    content_exists = content_path.exists() and content_path.is_file() and not content_path.is_symlink()

    if template_exists:
        template_text = _safe_read_text(template_path, DEFAULT_STATEMENT_TEMPLATE)
    else:
        template_text = DEFAULT_STATEMENT_TEMPLATE

    if content_exists:
        content_text = _safe_read_text(content_path, DEFAULT_STATEMENT_CONTENT)
    elif template_exists:
        content_text = DEFAULT_STATEMENT_CONTENT
    else:
        content_text = ""

    if not style_path.exists():
        style_path.write_text(DEFAULT_OLMPY_STY, encoding="utf-8")

    if STATEMENT_CONTENT_TOKEN in template_text:
        rendered = template_text.replace(STATEMENT_CONTENT_TOKEN, content_text)
    elif content_text.strip():
        rendered = template_text.rstrip() + "\n\n" + content_text.strip() + "\n"
    else:
        rendered = template_text
    rendered = _apply_problem_title(rendered, problem_title)

    main_path.write_text(rendered, encoding="utf-8")
    return main_path


def statement_sources_signature(workspace: Path, problem_title: str | None = None) -> str:
    """Stable signature of statement sources (excluding derived statement/main.tex)."""
    statement_root = workspace / STATEMENT_DIR
    hasher = hashlib.sha256()
    if not statement_root.exists() or not statement_root.is_dir() or statement_root.is_symlink():
        hasher.update(b"statement-missing")
        return hasher.hexdigest()

    files: list[tuple[str, Path]] = []
    for path in statement_root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
        except (OSError, ValueError):
            continue
        if rel == STATEMENT_MAIN_REL.as_posix():
            continue
        files.append((rel, path))
    files.sort(key=lambda item: item[0])

    for rel, path in files:
        hasher.update(rel.encode("utf-8", errors="replace"))
        hasher.update(b"\0")
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(SIGNATURE_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except OSError:
            hasher.update(b"[unreadable]")
        hasher.update(b"\0")
    if problem_title is not None:
        hasher.update(b"problem-title\0")
        hasher.update(str(problem_title or "").strip().encode("utf-8", errors="replace"))
        hasher.update(b"\0")
    return hasher.hexdigest()
