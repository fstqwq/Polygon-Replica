"""Runtime filesystem settings derived from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Absolute configured filesystem roots used by the local service."""

    db_path: Path
    bare_root: Path
    workspace_root: Path
    artifacts_root: Path
    cache_root: Path
    contest_source_root: Path
    backup_root: Path


def _configured_path(value: str) -> Path:
    """Make a path absolute without following a configured symlink."""

    configured = value.strip()
    if not configured:
        raise ValueError("configured filesystem path must not be empty")
    return Path(configured).expanduser().absolute()


def load_settings() -> Settings:
    """Load runtime settings from environment variables."""

    return Settings(
        db_path=_configured_path(
            os.getenv("POLYGON_REPLICA_DB", "/var/lib/polygon-replica/metadata.db")
        ),
        bare_root=_configured_path(
            os.getenv("POLYGON_REPLICA_BARE_ROOT", "/srv/polygon-replica/git")
        ),
        workspace_root=_configured_path(
            os.getenv("POLYGON_REPLICA_WORKSPACE_ROOT", "/srv/polygon-replica/workspaces")
        ),
        artifacts_root=_configured_path(
            os.getenv("POLYGON_REPLICA_ARTIFACTS_ROOT", "/srv/polygon-replica/export")
        ),
        cache_root=_configured_path(
            os.getenv("POLYGON_REPLICA_CACHE_ROOT", "/tmp/polygon-replica")
        ),
        contest_source_root=_configured_path(
            os.getenv(
                "POLYGON_REPLICA_CONTEST_SOURCE_ROOT",
                "/var/lib/polygon-replica/contest-sources",
            )
        ),
        backup_root=_configured_path(
            os.getenv(
                "POLYGON_REPLICA_BACKUP_ROOT",
                "/var/backups/polygon-replica",
            )
        ),
    )
