from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.service.platform.hashing import sha256_hex_json

_BUILD_REF_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ArtifactPaths:
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

    def compute_artifact_ref(self, payload: dict[str, object]) -> str:
        return sha256_hex_json(payload, ensure_ascii=True)

    def artifact_paths(self, artifact_ref: str) -> ArtifactPaths:
        safe_artifact_ref = self._normalize_artifact_ref(artifact_ref)
        root = self.artifacts_root / "objects" / safe_artifact_ref[:2] / safe_artifact_ref
        return ArtifactPaths(
            root=root,
            tests=root / "tests",
            ans=root / "ans",
            logs=root / "logs",
            bin=root / "bin",
            export=root / "export",
            statement_preview=root / "statement_preview",
        )

    def ensure_artifact_layout(self, artifact_ref: str) -> ArtifactPaths:
        paths = self.artifact_paths(artifact_ref)
        paths.root.mkdir(parents=True, exist_ok=True)
        for directory in (paths.tests, paths.ans, paths.logs, paths.bin, paths.export, paths.statement_preview):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def prepare_verification_root(self, verification_id: str) -> Path:
        path = self.resolve_verification_root(verification_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_verification_root(self, verification_id: str) -> Path:
        safe_verification_id = self._normalize_token(verification_id, field_name="verification_id")
        base = self.run_root.resolve()
        target = (base / safe_verification_id).resolve()
        if target != base and base not in target.parents:
            raise ValueError("verification_id escapes run_root")
        return target

    def prepare_verification_run_root(self, verification_id: str, run_id: str) -> Path:
        path = self.resolve_verification_run_root(verification_id, run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_verification_run_root(self, verification_id: str, run_id: str) -> Path:
        root = self.resolve_verification_root(verification_id)
        safe_run_id = self._normalize_token(run_id, field_name="run_id")
        target = (root / "runs" / safe_run_id).resolve()
        if root not in target.parents:
            raise ValueError("run_id escapes verification root")
        return target

    def _normalize_artifact_ref(self, artifact_ref: str) -> str:
        token = artifact_ref.strip().lower()
        if not _BUILD_REF_RE.fullmatch(token):
            raise ValueError("artifact_ref must be a 64-char lowercase hex digest")
        return token

    def _normalize_token(self, value: str, *, field_name: str) -> str:
        token = value.strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError(f"{field_name} has invalid format")
        return token

