from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import cast


@dataclass(frozen=True)
class PdfLatexResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int


def run_pdflatex(
    tex_name: str,
    *,
    cwd: Path,
    timeout_sec: int,
) -> PdfLatexResult:
    start = monotonic()
    proc = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_name],
        cwd=cwd,
        text=True,
        timeout=timeout_sec,
        capture_output=True,
        check=False,
    )
    return PdfLatexResult(
        returncode=int(proc.returncode),
        stdout=cast(str, proc.stdout or ""),
        stderr=cast(str, proc.stderr or ""),
        elapsed_ms=int((monotonic() - start) * 1000),
    )
