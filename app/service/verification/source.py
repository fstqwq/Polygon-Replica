from __future__ import annotations

import os
from pathlib import Path


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
