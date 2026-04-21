"""Runtime filesystem settings derived from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Resolved filesystem roots used by the local service."""

    db_path: Path
    bare_root: Path
    workspace_root: Path
    artifacts_root: Path
    cache_root: Path

def load_settings() -> Settings:
    """Load runtime settings from environment variables."""

    return Settings(
        db_path=Path(
            os.getenv("POLYGON_REPLICA_DB", "/var/lib/polygon-replica/metadata.db")
        ).resolve(),
        bare_root=Path(
            os.getenv("POLYGON_REPLICA_BARE_ROOT", "/srv/polygon-replica/git")
        ).resolve(),
        workspace_root=Path(
            os.getenv("POLYGON_REPLICA_WORKSPACE_ROOT", "/srv/polygon-replica/workspaces")
        ).resolve(),
        artifacts_root=Path(
            os.getenv("POLYGON_REPLICA_ARTIFACTS_ROOT", "/srv/polygon-replica/export")
        ).resolve(),
        cache_root=Path(os.getenv("POLYGON_REPLICA_CACHE_ROOT", "/tmp/polygon-replica")).resolve(),
    )
