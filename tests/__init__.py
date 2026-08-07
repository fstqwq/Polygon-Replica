from __future__ import annotations

import os
import uuid
from pathlib import Path


_TESTSUITE_BASE = Path("/tmp/polygon-replica")
_TESTSUITE_ROOT = Path(
    os.environ.get(
        "POLYGON_REPLICA_TESTSUITE_ROOT",
        str(_TESTSUITE_BASE / f"testsuite-{uuid.uuid4().hex[:8]}"),
    )
).resolve()

os.environ["POLYGON_REPLICA_TESTSUITE_ROOT"] = str(_TESTSUITE_ROOT)
os.environ["POLYGON_REPLICA_DB"] = str(
    _TESTSUITE_ROOT / "var" / "lib" / "polygon-replica" / "metadata.db"
)
os.environ["POLYGON_REPLICA_BARE_ROOT"] = str(_TESTSUITE_ROOT / "srv" / "git")
os.environ["POLYGON_REPLICA_WORKSPACE_ROOT"] = str(
    _TESTSUITE_ROOT / "srv" / "workspaces"
)
os.environ["POLYGON_REPLICA_RUN_ROOT"] = str(_TESTSUITE_ROOT / "srv" / "runs")
os.environ["POLYGON_REPLICA_ARTIFACTS_ROOT"] = str(
    _TESTSUITE_ROOT / "var" / "lib" / "polygon-replica" / "artifacts"
)
os.environ["POLYGON_REPLICA_CACHE_ROOT"] = str(
    _TESTSUITE_ROOT / "var" / "cache" / "polygon-replica"
)
os.environ["POLYGON_REPLICA_CONTEST_SOURCE_ROOT"] = str(
    _TESTSUITE_ROOT / "var" / "lib" / "polygon-replica" / "contest-sources"
)
os.environ["POLYGON_REPLICA_BACKUP_ROOT"] = str(
    _TESTSUITE_ROOT / "var" / "backups" / "polygon-replica"
)
os.environ["POLYGON_REPLICA_AUTH_COOKIE_SECURE"] = "1"
