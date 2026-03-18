from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    bare_root: Path
    workspace_root: Path
    run_root: Path
    artifacts_root: Path
    cache_root: Path



def load_settings() -> Settings:
    return Settings(
        db_path=Path(os.getenv("POLYGON_REPLICA_DB", "/var/lib/polygon-replica/metadata.db")).resolve(),
        bare_root=Path(os.getenv("POLYGON_REPLICA_BARE_ROOT", "/srv/git")).resolve(),
        workspace_root=Path(os.getenv("POLYGON_REPLICA_WORKSPACE_ROOT", "/srv/workspaces")).resolve(),
        run_root=Path(os.getenv("POLYGON_REPLICA_RUN_ROOT", "/srv/runs")).resolve(),
        artifacts_root=Path(os.getenv("POLYGON_REPLICA_ARTIFACTS_ROOT", "/var/lib/polygon-replica/artifacts")).resolve(),
        cache_root=Path(os.getenv("POLYGON_REPLICA_CACHE_ROOT", "/var/cache/polygon-replica")).resolve(),
    )
