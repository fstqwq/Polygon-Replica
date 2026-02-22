from __future__ import annotations

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
        for p in sorted(paths.root.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                files.append(
                    {
                        "path": str(p.relative_to(paths.root)),
                        "sha256": sha256_file(p),
                        "size": p.stat().st_size,
                    }
                )
        summary = {
            "file_count": len(files),
            "total_size": sum(f["size"] for f in files),
            "tests_count": len(list(paths.tests.glob("*"))),
            "ans_count": len(list(paths.ans.glob("*"))),
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
