"""Hash-based identity and installation for vendored standard checkers."""

import hashlib
import os
from functools import cache
from pathlib import Path

from app.service.platform.workspace_path import safe_workspace_path


def _standard_checker_root() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "third_party"
        / "testlib"
        / "checkers"
    ).resolve()


def _build_hash_map(root: Path | None = None) -> dict[str, str]:
    checker_root = root if root is not None else _standard_checker_root()
    result: dict[str, str] = {}
    try:
        with os.scandir(checker_root) as entries:
            for entry in entries:
                if not entry.name.endswith(".cpp"):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                except OSError:
                    continue
                try:
                    payload = (checker_root / entry.name).read_bytes()
                except OSError:
                    continue
                result[hashlib.sha256(payload).hexdigest()] = entry.name
    except OSError:
        pass
    return result


@cache
def standard_checker_hash_map() -> dict[str, str]:
    """Return the cached mapping from source digest to checker filename."""

    return _build_hash_map()


def detect_standard_checker(source_path: Path) -> str | None:
    """Return the vendored checker filename for byte-identical source."""

    try:
        payload = source_path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    return standard_checker_hash_map().get(digest)


def copy_standard_checker(name: str, workspace: Path) -> str:
    """Install one vendored checker and return its repository-relative path."""

    token = name.strip()
    if token.startswith("std::"):
        token = token[5:]
    if not token.endswith(".cpp"):
        token += ".cpp"
    root = _standard_checker_root()
    source = (root / token).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"invalid standard checker name: {name}") from exc
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"standard checker not found: {name}")
    target_dir = safe_workspace_path(workspace, "checkers")
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = safe_workspace_path(workspace, f"checkers/{token}")
    destination.write_text(
        source.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    return f"checkers/{token}"
