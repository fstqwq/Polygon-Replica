from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class VerificationLayout:
    root: Path
    logs: Path


@dataclass(frozen=True)
class PreviewLayout:
    root: Path
    logs: Path
    statement_preview: Path


class FsManager:
    def __init__(self, cache_root: Path, artifacts_root: Path):
        self.cache_root = cache_root
        self.artifacts_root = artifacts_root
        self.cache_artifacts_root = self.cache_root / "artifacts"
        self.runtime_root = self.cache_root / "runtime"
        self.verification_root = self.cache_artifacts_root / "verifications"
        self.preview_root = self.cache_artifacts_root / "previews"
        self.snapshot_root = self.runtime_root / "snapshots"

    def resolve_verification_root(self, verification_id: str) -> Path:
        safe_verification_id = self._normalize_token(verification_id, field_name="verification_id")
        base = self.verification_root.resolve()
        target = (base / safe_verification_id).resolve()
        if target != base and base not in target.parents:
            raise ValueError("verification_id escapes verification root")
        return target

    def prepare_verification_root(self, verification_id: str) -> Path:
        path = self.resolve_verification_root(verification_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def verification_layout(self, verification_id: str) -> VerificationLayout:
        root = self.resolve_verification_root(verification_id)
        return VerificationLayout(
            root=root,
            logs=root / "logs",
        )

    def prepare_verification_layout(self, verification_id: str) -> VerificationLayout:
        layout = self.verification_layout(verification_id)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.logs.mkdir(parents=True, exist_ok=True)
        return layout

    def resolve_preview_root(self, preview_id: str) -> Path:
        safe_preview_id = self._normalize_token(preview_id, field_name="preview_id")
        base = self.preview_root.resolve()
        target = (base / safe_preview_id).resolve()
        if target != base and base not in target.parents:
            raise ValueError("preview_id escapes preview root")
        return target

    def preview_layout(self, preview_id: str) -> PreviewLayout:
        root = self.resolve_preview_root(preview_id)
        return PreviewLayout(
            root=root,
            logs=root / "logs",
            statement_preview=root / "statement_preview",
        )

    def prepare_preview_layout(self, preview_id: str) -> PreviewLayout:
        layout = self.preview_layout(preview_id)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.logs.mkdir(parents=True, exist_ok=True)
        layout.statement_preview.mkdir(parents=True, exist_ok=True)
        return layout

    def _normalize_token(self, value: str, *, field_name: str) -> str:
        token = value.strip()
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError(f"{field_name} has invalid format")
        return token
