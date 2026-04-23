from __future__ import annotations

import os
from pathlib import Path

from app.service.platform.hashing import quick_fp_digest, sha256_file


_VERIFICATION_SIGNATURE_FILE_TARGETS: tuple[str, ...] = (
    "config/problem.json",
    "config/build.json",
    "tests/spec.json",
)

_VERIFICATION_SIGNATURE_DIR_TARGETS: tuple[str, ...] = (
    "generators",
    "validators",
    "checkers",
    "solutions",
    "tests/manual",
    "tests/generator",
    "third_party/testlib",
)


def verification_signature(workspace: Path) -> str:
    entries: list[dict[str, object]] = []
    try:
        workspace_resolved = workspace.resolve()
    except OSError:
        workspace_resolved = workspace

    def _is_within_workspace(path: Path) -> bool:
        return workspace_resolved == path or workspace_resolved in path.parents

    def _safe_file(rel_path: str) -> Path | None:
        target = workspace / rel_path
        try:
            if target.is_symlink() or (not target.exists()) or (not target.is_file()):
                return None
            resolved = target.resolve()
        except OSError:
            return None
        if not _is_within_workspace(resolved):
            return None
        return target

    def _hash_file(rel_path: str) -> None:
        target = _safe_file(rel_path)
        if target is None:
            entries.append({"kind": "file", "target": rel_path, "state": "missing"})
            return
        try:
            stat_obj = target.stat()
            entries.append(
                {
                    "kind": "file",
                    "target": rel_path,
                    "state": "ok",
                    "size": int(stat_obj.st_size),
                    "sha256": sha256_file(target),
                }
            )
        except OSError:
            entries.append({"kind": "file", "target": rel_path, "state": "unreadable"})

    def _hash_dir(rel_dir: str) -> None:
        root = workspace / rel_dir
        try:
            if root.is_symlink() or (not root.exists()) or (not root.is_dir()):
                entries.append({"kind": "dir", "target": rel_dir, "state": "missing"})
                return
            root_resolved = root.resolve()
        except OSError:
            entries.append({"kind": "dir", "target": rel_dir, "state": "missing"})
            return
        if not _is_within_workspace(root_resolved):
            entries.append({"kind": "dir", "target": rel_dir, "state": "invalid"})
            return
        entries.append({"kind": "dir", "target": rel_dir, "state": "ok"})
        files: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if not _is_within_workspace(dir_root_resolved):
                dirnames[:] = []
                continue
            safe_dirs: list[str] = []
            for name in dirnames:
                child = dir_root / name
                try:
                    if child.is_symlink() or (not child.exists()) or (not child.is_dir()):
                        continue
                except OSError:
                    continue
                safe_dirs.append(name)
            dirnames[:] = sorted(safe_dirs)
            for name in sorted(filenames):
                path = dir_root / name
                try:
                    if path.is_symlink() or (not path.exists()) or (not path.is_file()):
                        continue
                    path_resolved = path.resolve()
                except OSError:
                    continue
                if not _is_within_workspace(path_resolved):
                    continue
                try:
                    rel = path.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                files.append((rel, path))
        files.sort(key=lambda item: item[0])
        for rel, path in files:
            try:
                stat_obj = path.stat()
                entries.append(
                    {
                        "kind": "dir-file",
                        "target": rel_dir,
                        "path": rel,
                        "state": "ok",
                        "size": int(stat_obj.st_size),
                        "sha256": sha256_file(path),
                    }
                )
            except OSError:
                entries.append({"kind": "dir-file", "target": rel_dir, "path": rel, "state": "unreadable"})

    for rel_path in _VERIFICATION_SIGNATURE_FILE_TARGETS:
        _hash_file(rel_path)
    for rel_dir in _VERIFICATION_SIGNATURE_DIR_TARGETS:
        _hash_dir(rel_dir)
    return quick_fp_digest(entries, schema="verification-signature")
