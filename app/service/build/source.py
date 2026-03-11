from __future__ import annotations

import os
from pathlib import Path
import re


STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_safe_source_in_dir(root: Path, path: Path, root_resolved: Path | None = None) -> bool:
    if path.is_symlink() or not path.exists() or not path.is_file():
        return False
    try:
        resolved_root = root_resolved if root_resolved is not None else root.resolve()
        resolved = path.resolve()
    except OSError:
        return False
    return resolved_root in resolved.parents or resolved_root == resolved


def find_source_with_extensions(
    root: Path,
    folder: str,
    extensions: tuple[str, ...],
    preferred: str | None = None,
) -> Path | None:
    base = root / folder
    if not base.exists() or not base.is_dir():
        return None
    try:
        base_resolved = base.resolve()
    except OSError:
        return None
    if preferred:
        exact = base / preferred
        if is_safe_source_in_dir(base, exact, root_resolved=base_resolved):
            return exact
        stem = Path(preferred).stem
        for ext in extensions:
            candidate = base / f"{stem}{ext}"
            if is_safe_source_in_dir(base, candidate, root_resolved=base_resolved):
                return candidate
    try:
        best: Path | None = None
        best_name = ""
        with os.scandir(base) as entries:
            for entry in entries:
                name = entry.name
                if Path(name).suffix.lower() not in extensions:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if best is None or name < best_name:
                    best = base / name
                    best_name = name
    except OSError:
        return None
    return best


def resolve_source(snapshot: Path, rel_path: str, snapshot_resolved: Path | None = None) -> Path:
    resolved_snapshot = snapshot_resolved if snapshot_resolved is not None else snapshot.resolve()
    p = (snapshot / rel_path).resolve()
    if resolved_snapshot not in p.parents:
        raise RuntimeError(f"invalid configured source path: {rel_path}")
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"configured source does not exist: {rel_path}")
    return p


def normalize_standard_checker_name(raw: str, name_pattern=None) -> str:
    value = str(raw or "").strip()
    if value.startswith("std::"):
        value = value[5:]
    if not value:
        raise RuntimeError("checker_standard is empty")
    if "/" in value or "\\" in value:
        raise RuntimeError("checker_standard is invalid")
    if not value.endswith(".cpp"):
        value += ".cpp"
    pattern = name_pattern if name_pattern is not None else STANDARD_CHECKER_NAME_RE
    if not pattern.fullmatch(value):
        raise RuntimeError("checker_standard is invalid")
    return value


def resolve_standard_checker_source(
    checker_standard: str,
    *,
    checker_root: Path | None = None,
    standard_checker_root: Path | None = None,
    standard_checker_name_re=None,
) -> Path | None:
    root = standard_checker_root if standard_checker_root is not None else checker_root
    if root is None:
        raise RuntimeError("checker root is required")
    raw = str(checker_standard or "").strip()
    if not raw:
        return None
    checker_name = normalize_standard_checker_name(raw)
    name_re = standard_checker_name_re if standard_checker_name_re is not None else STANDARD_CHECKER_NAME_RE
    if not name_re.fullmatch(checker_name):
        raise RuntimeError("checker_standard is invalid")
    source = (root / checker_name).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        raise RuntimeError("checker_standard is invalid")
    try:
        if source.is_symlink() or not source.exists() or not source.is_file():
            raise RuntimeError(f"configured standard checker does not exist: std::{checker_name}")
    except OSError:
        raise RuntimeError("standard checker catalog is unavailable")
    return source


def select_source(
    snapshot: Path,
    build_cfg: dict,
    config_key: str,
    folder: str,
    *,
    cpp_extensions: tuple[str, ...],
    preferred: str | None = None,
    snapshot_resolved: Path | None = None,
) -> Path | None:
    configured = build_cfg.get(config_key)
    if configured:
        return resolve_source(snapshot, str(configured), snapshot_resolved=snapshot_resolved)
    return find_source_with_extensions(snapshot, folder, cpp_extensions, preferred=preferred)


def select_checker_source(
    snapshot: Path,
    build_cfg: dict,
    *,
    standard_checker_root: Path,
    standard_checker_name_re,
    cpp_extensions: tuple[str, ...],
    snapshot_resolved: Path | None = None,
) -> Path | None:
    standard_source = resolve_standard_checker_source(
        str(build_cfg.get("checker_standard") or ""),
        standard_checker_root=standard_checker_root,
        standard_checker_name_re=standard_checker_name_re,
    )
    if standard_source is not None:
        return standard_source
    return select_source(
        snapshot,
        build_cfg,
        "checker_source",
        "checkers",
        cpp_extensions=cpp_extensions,
        snapshot_resolved=snapshot_resolved,
    )
