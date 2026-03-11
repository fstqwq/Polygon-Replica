from __future__ import annotations

from pathlib import Path
from typing import Callable


def files_equal(lhs: Path, rhs: Path) -> bool:
    if not lhs.exists() or not rhs.exists():
        return False
    if lhs.stat().st_size != rhs.stat().st_size:
        return False
    chunk = 1024 * 1024
    with lhs.open("rb") as fa, rhs.open("rb") as fb:
        while True:
            a = fa.read(chunk)
            b = fb.read(chunk)
            if a != b:
                return False
            if not a:
                return True


def feedback_message_for_pass(
    pass_feedback_dir: Path,
    base_root: Path,
    *,
    is_safe_regular_file: Callable[[Path, Path], bool],
    compact_inline_error: Callable[[object], str],
) -> str:
    candidates = ("judgemessage.txt", "teammessage.txt", "checker.log")
    for name in candidates:
        path = pass_feedback_dir / name
        if not is_safe_regular_file(base_root, path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = compact_inline_error(raw_line)
            if line:
                return line
    return ""


