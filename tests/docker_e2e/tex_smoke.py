from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(
    0,
    os.environ.get("POLYGON_REPLICA_E2E_REPO_ROOT", "/opt/polygon-replica"),
)

from app.service.statement.tex_compile import TexCompileService  # noqa: E402


SOURCES = (
    (
        "pdflatex",
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Polygon Replica Docker TeX smoke.\n"
        "\\end{document}\n",
    ),
    (
        "xelatex",
        "\\documentclass{article}\n"
        "\\usepackage{fontspec}\n"
        "\\usepackage{xeCJK}\n"
        "\\setmainfont{TeX Gyre Termes}\n"
        "\\setsansfont{TeX Gyre Heros}\n"
        "\\setCJKmainfont{Noto Serif CJK SC}\n"
        "\\begin{document}\n"
        "Polygon Replica Docker TeX smoke. 中文。\n"
        "\\end{document}\n",
    ),
)


def _compile(service: TexCompileService, root: Path, engine: str, source_text: str) -> None:
    workdir = root / engine
    workdir.mkdir()
    source = workdir / "main.tex"
    source.write_text(source_text, encoding="utf-8")
    result = service.compile_pdf(source)
    if result.engine != engine:
        raise RuntimeError(
            f"Docker TeX smoke selected {result.engine!r}, expected {engine!r}"
        )
    if result.proc.status != "ok" or result.proc.returncode != 0:
        raise RuntimeError(
            f"Docker {engine} sandbox smoke failed: "
            f"status={result.proc.status} returncode={result.proc.returncode}\n"
            f"{result.log_text}"
        )
    if not result.pdf_path.is_file() or result.pdf_path.stat().st_size <= 0:
        raise RuntimeError(f"Docker {engine} sandbox smoke did not produce a PDF")
    if not result.pdf_path.read_bytes().startswith(b"%PDF-"):
        raise RuntimeError(f"Docker {engine} sandbox smoke produced an invalid PDF")
    if result.proc.details.get("root_switched") is not True:
        raise RuntimeError(f"Docker {engine} smoke did not use the sandbox root switch")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="polygon-replica-tex-smoke-") as raw:
        service = TexCompileService()
        for engine, source_text in SOURCES:
            _compile(service, Path(raw), engine, source_text)
    print("Docker pdflatex/xelatex sandbox smoke passed.")


if __name__ == "__main__":
    main()
