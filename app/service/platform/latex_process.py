from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import cast

import re

_XELATEX_RE = re.compile(r"\\usepackage\s*\{(?:fontspec|xeCJK)\}")


def detect_latex_engine(tex_path: Path) -> str:
    """Return ``"xelatex"`` if *tex_path* contains xelatex-only packages, else ``"pdflatex"``."""
    try:
        text = tex_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "pdflatex"
    if _XELATEX_RE.search(text):
        return "xelatex"
    return "pdflatex"


@dataclass(frozen=True)
class LatexResult:
    engine: str
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int


def run_latex(
    tex_name: str,
    *,
    cwd: Path,
    timeout_sec: int,
    engine: str = "pdflatex",
) -> LatexResult:
    start = monotonic()
    proc = subprocess.run(
        [engine, "-interaction=nonstopmode", "-halt-on-error", tex_name],
        cwd=cwd,
        text=True,
        timeout=timeout_sec,
        capture_output=True,
        check=False,
    )
    return LatexResult(
        engine=engine,
        returncode=int(proc.returncode),
        stdout=cast(str, proc.stdout or ""),
        stderr=cast(str, proc.stderr or ""),
        elapsed_ms=int((monotonic() - start) * 1000),
    )


# Backward-compatible alias
PdfLatexResult = LatexResult


def run_pdflatex(
    tex_name: str,
    *,
    cwd: Path,
    timeout_sec: int,
) -> LatexResult:
    return run_latex(tex_name, cwd=cwd, timeout_sec=timeout_sec, engine="pdflatex")
