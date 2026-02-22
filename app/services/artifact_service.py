from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.services.util import ensure_dir, sha256_file, write_json


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
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            keep_dirs: list[str] = []
            for name in sorted(dirnames):
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = keep_dirs
            for name in sorted(filenames):
                p = dir_root / name
                if p.is_symlink():
                    continue
                if not p.is_file():
                    continue
                yield p

    def prepare(self, problem_slug: str, build_id: str) -> ArtifactPaths:
        root = self.artifacts_root / problem_slug / build_id
        tests = ensure_dir(root / "tests")
        ans = ensure_dir(root / "ans")
        logs = ensure_dir(root / "logs")
        statement_preview = ensure_dir(root / "statement_preview")
        export = ensure_dir(root / "export")
        return ArtifactPaths(root, tests, ans, logs, statement_preview, export)

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
        for p in self._iter_manifest_files(paths.root):
            rel = p.relative_to(paths.root)
            if rel == Path("manifest.json"):
                continue
            size = p.stat().st_size
            files.append(
                {
                    "path": str(rel),
                    "sha256": sha256_file(p),
                    "size": size,
                }
            )
            file_count += 1
            total_size += size
        summary = {
            "file_count": file_count,
            "total_size": total_size,
            "tests_count": sum(1 for _ in paths.tests.iterdir()) if paths.tests.exists() else 0,
            "ans_count": sum(1 for _ in paths.ans.iterdir()) if paths.ans.exists() else 0,
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
