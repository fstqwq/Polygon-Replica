from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.service.platform.hashing import sha256_hex_json

_BUILD_REF_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class BuildPaths:
    root: Path
    tests: Path
    ans: Path
    logs: Path
    bin: Path
    export: Path
    statement_preview: Path


class FsManager:
    def __init__(self, artifacts_root: Path, run_root: Path):
        self.artifacts_root = artifacts_root
        self.run_root = run_root

    def compute_build_ref(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        return sha256_hex_json(payload, ensure_ascii=True)

    def build_paths(self, build_ref: str) -> BuildPaths:
        safe_build_ref = self._normalize_build_ref(build_ref)
        root = self.artifacts_root / "objects" / safe_build_ref[:2] / safe_build_ref
        return BuildPaths(
            root=root,
            tests=root / "tests",
            ans=root / "ans",
            logs=root / "logs",
            bin=root / "bin",
            export=root / "export",
            statement_preview=root / "statement_preview",
        )

    def ensure_build_layout(self, build_ref: str) -> BuildPaths:
        paths = self.build_paths(build_ref)
        paths.root.mkdir(parents=True, exist_ok=True)
        for directory in (paths.tests, paths.ans, paths.logs, paths.bin, paths.export, paths.statement_preview):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def prepare_run_root(self, run_id: str) -> Path:
        path = self.resolve_run_root(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_run_root(self, run_id: str) -> Path:
        safe_run_id = self._normalize_token(run_id, field_name="run_id")
        base = self.run_root.resolve()
        target = (base / safe_run_id).resolve()
        if target != base and base not in target.parents:
            raise ValueError("run_id escapes run_root")
        return target

    def _normalize_build_ref(self, build_ref: str) -> str:
        token = str(build_ref or "").strip().lower()
        if not _BUILD_REF_RE.fullmatch(token):
            raise ValueError("build_ref must be a 64-char lowercase hex digest")
        return token

    def _normalize_token(self, value: str, *, field_name: str) -> str:
        token = str(value or "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError(f"{field_name} has invalid format")
        return token



