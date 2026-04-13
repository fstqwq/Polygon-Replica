import re
from pathlib import Path

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
