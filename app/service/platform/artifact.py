from __future__ import annotations

from pathlib import Path


class ArtifactService:
    def __init__(self, artifacts_root: Path):
        self.artifacts_root = artifacts_root

