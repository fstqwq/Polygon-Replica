"""Standard checker hash detection and copy utilities.

Provides hash-based detection of whether a checker source file matches
a known standard testlib checker, and a utility to copy standard checkers
into a problem repository.
"""
import hashlib
import os
from pathlib import Path

from app.service.platform.workspace_path import safe_workspace_path


def _standard_checker_root() -> Path:
    return (Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "checkers").resolve()


_HASH_MAP: dict[str, str] | None = None


def _build_hash_map(root: Path | None = None) -> dict[str, str]:
    """Build {sha256hex: filename} from the upstream checkers directory."""
    checker_root = root if root is not None else _standard_checker_root()
    result: dict[str, str] = {}
    try:
        with os.scandir(checker_root) as entries:
            for entry in entries:
                if not entry.name.endswith(".cpp"):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                try:
                    blob = (checker_root / entry.name).read_bytes()
                except OSError:
                    continue
                h = hashlib.sha256(blob).hexdigest()
                result[h] = entry.name
    except OSError:
        pass
    return result


def standard_checker_hash_map() -> dict[str, str]:
    """Return cached {sha256hex: filename} map of upstream standard checkers."""
    global _HASH_MAP
    if _HASH_MAP is None:
        _HASH_MAP = _build_hash_map()
    return _HASH_MAP


def detect_standard_checker(source_path: Path) -> str | None:
    """If *source_path* is byte-identical to a standard checker, return its filename (e.g. ``"wcmp.cpp"``).

    Returns ``None`` if the file doesn't match any known standard checker.
    """
    try:
        blob = source_path.read_bytes()
    except OSError:
        return None
    h = hashlib.sha256(blob).hexdigest()
    return standard_checker_hash_map().get(h)


def copy_standard_checker(name: str, workspace: Path) -> str:
    """Copy a standard checker into *workspace* and return the repo-relative path.

    *name* can be ``"wcmp"``, ``"wcmp.cpp"``, or ``"std::wcmp.cpp"``.
    Returns a path like ``"checkers/wcmp.cpp"``.
    """
    token = name.strip()
    if token.startswith("std::"):
        token = token[5:]
    if not token.endswith(".cpp"):
        token += ".cpp"
    source = (_standard_checker_root() / token).resolve()
    root = _standard_checker_root()
    try:
        source.relative_to(root)
    except ValueError:
        raise ValueError(f"invalid standard checker name: {name}")
    if source.is_symlink() or not source.exists() or not source.is_file():
        raise ValueError(f"standard checker not found: {name}")
    target_dir = safe_workspace_path(workspace, "checkers")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = safe_workspace_path(workspace, f"checkers/{token}")
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return f"checkers/{token}"
