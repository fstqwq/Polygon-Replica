from __future__ import annotations

import os
from pathlib import Path


def contains_symlink_component(root: Path, candidate: Path) -> bool:
    try:
        if root.is_symlink():
            return True
    except OSError:
        return True
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return True
    cur = root
    for part in rel.parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True
        if not cur.exists():
            break
    return False


def is_safe_path_within(root: Path, path: Path, root_resolved: Path | None = None) -> bool:
    try:
        resolved_root = root_resolved if root_resolved is not None else root.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return resolved_root in resolved.parents or resolved_root == resolved


def is_safe_dir(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.exists() or not path.is_dir():
        return False
    return is_safe_path_within(root, path)


def is_safe_regular_file(root: Path, path: Path, root_resolved: Path | None = None) -> bool:
    if path.is_symlink() or not path.exists() or not path.is_file():
        return False
    return is_safe_path_within(root, path, root_resolved=root_resolved)


def safe_top_level_suffix_names(root: Path, suffix: str) -> list[str]:
    if not suffix:
        return []
    matched: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(suffix):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                matched.append(name)
    except OSError:
        return []
    matched.sort()
    return matched


