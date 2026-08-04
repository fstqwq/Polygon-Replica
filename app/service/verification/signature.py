from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.service.platform.git_process import run_git
from app.service.platform.hashing import quick_fp_digest, sha256_file, sha256_hex_text
from app.service.platform.runtime_blob_store import PayloadFile


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


@dataclass(frozen=True, slots=True)
class VerificationManifest:
    signature: str
    files: dict[str, PayloadFile]

    def require(self, relative_path: str) -> PayloadFile:
        payload = self.files.get(relative_path)
        if payload is None:
            raise RuntimeError(f"verification manifest file is missing: {relative_path}")
        return payload


def git_blob_identities(workspace: Path, commit: str) -> dict[str, str]:
    args = [
        "git",
        "-C",
        str(workspace),
        "ls-tree",
        "-r",
        "--full-tree",
        commit,
        "--",
        *_VERIFICATION_SIGNATURE_FILE_TARGETS,
        *_VERIFICATION_SIGNATURE_DIR_TARGETS,
    ]
    result = run_git(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to inspect verification Git blobs")
    identities: dict[str, str] = {}
    for line in result.stdout.splitlines():
        metadata, separator, relative_path = line.partition("\t")
        if not separator:
            continue
        parts = metadata.split()
        if len(parts) != 3 or parts[1] != "blob" or re.fullmatch(r"[0-9a-f]+", parts[2]) is None:
            continue
        identities[relative_path] = sha256_hex_text(f"git-blob\0{parts[2]}")
    return identities


def _stat_mtime_ns(stat_obj: os.stat_result) -> int:
    return int(getattr(stat_obj, "st_mtime_ns", int(float(stat_obj.st_mtime) * 1_000_000_000)))


def _verification_source_entries(workspace: Path, *, hash_content: bool) -> list[dict[str, object]]:
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

    def _file_entry(kind: str, path: Path, stat_obj: os.stat_result) -> dict[str, object]:
        entry: dict[str, object] = {
            "kind": kind,
            "state": "ok",
            "size": int(stat_obj.st_size),
        }
        if hash_content:
            entry["sha256"] = sha256_file(path)
        else:
            entry["mtime_ns"] = _stat_mtime_ns(stat_obj)
        return entry

    def _hash_file(rel_path: str) -> None:
        target = _safe_file(rel_path)
        if target is None:
            entries.append({"kind": "file", "target": rel_path, "state": "missing"})
            return
        try:
            stat_obj = target.stat()
            entry = _file_entry("file", target, stat_obj)
            entry["target"] = rel_path
            entries.append(entry)
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
                entry = _file_entry("dir-file", path, stat_obj)
                entry["target"] = rel_dir
                entry["path"] = rel
                entries.append(entry)
            except OSError:
                entries.append({"kind": "dir-file", "target": rel_dir, "path": rel, "state": "unreadable"})

    for rel_path in _VERIFICATION_SIGNATURE_FILE_TARGETS:
        _hash_file(rel_path)
    for rel_dir in _VERIFICATION_SIGNATURE_DIR_TARGETS:
        _hash_dir(rel_dir)
    return entries


def verification_signature(workspace: Path) -> str:
    return quick_fp_digest(
        _verification_source_entries(workspace, hash_content=True),
        schema="verification-signature",
    )


def verification_manifest(
    snapshot: Path,
    *,
    git_identities: dict[str, str] | None = None,
) -> VerificationManifest:
    raw_entries = _verification_source_entries(snapshot, hash_content=False)
    manifest_entries: list[dict[str, object]] = []
    files: dict[str, PayloadFile] = {}
    identities = {} if git_identities is None else git_identities
    for raw in raw_entries:
        entry = dict(raw)
        entry.pop("mtime_ns", None)
        relative_path = ""
        if entry.get("state") == "ok" and entry.get("kind") == "file":
            relative_path = str(entry["target"])
        elif entry.get("state") == "ok" and entry.get("kind") == "dir-file":
            relative_path = str(entry["path"])
        if relative_path:
            path = (snapshot / relative_path).resolve()
            identity = identities.get(relative_path)
            if identity is None:
                identity = sha256_file(path, chunk_size=16 * 1024 * 1024)
            payload = PayloadFile(
                path=path,
                size=int(entry["size"]),
                identity=identity,
            )
            files[relative_path] = payload
            entry["identity"] = identity
        manifest_entries.append(entry)
    signature = quick_fp_digest(manifest_entries, schema="verification-manifest")
    return VerificationManifest(signature=signature, files=files)


def verification_fingerprint(workspace: Path) -> str:
    return quick_fp_digest(
        _verification_source_entries(workspace, hash_content=False),
        schema="verification-fingerprint",
    )
