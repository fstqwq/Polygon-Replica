from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.services.hashing import sha256_file
from app.services.util import write_json


@dataclass
class ArtifactPaths:
    root: Path
    tests: Path
    ans: Path
    logs: Path
    statement_preview: Path
    export: Path


class ArtifactService:
    def __init__(self, artifacts_root: Path):
        self.artifacts_root = artifacts_root

    def _iter_manifest_files(self, root: Path):
        if root.is_symlink():
            return
        try:
            root_resolved = root.resolve()
        except OSError:
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if root_resolved not in dir_root_resolved.parents and root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            try:
                rel_root = dir_root.relative_to(root)
            except ValueError:
                dirnames[:] = []
                continue
            rel_prefix = "" if rel_root == Path(".") else rel_root.as_posix()
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            safe_filenames: list[str] = []
            for name in filenames:
                p = dir_root / name
                if p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                safe_filenames.append(name)

            for name in sorted(safe_filenames):
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                yield rel, dir_root / name

    def write_manifest(
        self,
        paths: ArtifactPaths,
        source_commit: str,
        source_ref: str,
        toolchain_digest: str,
        seed: int,
        generation_params: dict,
        steps: list[dict],
    ) -> None:
        files: list[dict] = []
        file_count = 0
        total_size = 0
        tests_count = 0
        ans_count = 0
        for rel, p in self._iter_manifest_files(paths.root):
            if rel == "manifest.json":
                continue
            if rel == "tests" or rel.startswith("tests/"):
                tests_count += 1
            elif rel == "ans" or rel.startswith("ans/"):
                ans_count += 1
            size = p.stat().st_size
            files.append(
                {
                    "path": rel,
                    "sha256": sha256_file(p),
                    "size": size,
                }
            )
            file_count += 1
            total_size += size
        summary = {
            "file_count": file_count,
            "total_size": total_size,
            "tests_count": tests_count,
            "ans_count": ans_count,
        }
        write_json(
            paths.root / "manifest.json",
            {
                "source": {"commit": source_commit, "ref": source_ref},
                "toolchain": {"digest": toolchain_digest},
                "seed": seed,
                "generation_params": generation_params,
                "files": files,
                "summary": summary,
                "steps": steps,
            },
        )
