from __future__ import annotations

import atexit
import os
import shutil
import unittest
import uuid
from pathlib import Path

from app.db import DB
from app.config import build_config_values
from app.service.platform.fs.layout import FsManager
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.repository.workspace import WorkspaceService
from app.service.verification.task_store import VerificationTaskStore
from app.setting import Settings


_PROCESS_ROOT = Path(
    os.environ.get(
        "POLYGON_REPLICA_TESTSUITE_ROOT",
        f"/tmp/polygon-replica/db-testsuite-{uuid.uuid4().hex[:8]}",
    )
).resolve()
_DB_PATH = _PROCESS_ROOT / "metadata.db"
_DB_TEMPLATE = _PROCESS_ROOT / "metadata.template.db"


def _settings() -> Settings:
    return Settings(
        db_path=_DB_PATH,
        bare_root=_PROCESS_ROOT / "git",
        workspace_root=_PROCESS_ROOT / "workspaces",
        artifacts_root=_PROCESS_ROOT / "artifacts",
        cache_root=_PROCESS_ROOT / "cache",
        contest_source_root=_PROCESS_ROOT / "contest-sources",
        backup_root=_PROCESS_ROOT / "backups",
    )


def _remove_database_sidecars() -> None:
    Path(f"{_DB_PATH}-wal").unlink(missing_ok=True)
    Path(f"{_DB_PATH}-shm").unlink(missing_ok=True)


def _create_template() -> None:
    _PROCESS_ROOT.mkdir(parents=True, exist_ok=True)
    database = DB(_DB_PATH, config_values=build_config_values())
    database.init()
    with database.conn() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(_DB_PATH, _DB_TEMPLATE)


def _restore_template() -> None:
    _remove_database_sidecars()
    replacement = _PROCESS_ROOT / f"metadata-{uuid.uuid4().hex}.db"
    try:
        shutil.copy2(_DB_TEMPLATE, replacement)
        os.replace(replacement, _DB_PATH)
    finally:
        replacement.unlink(missing_ok=True)


def _cleanup() -> None:
    shutil.rmtree(_PROCESS_ROOT, ignore_errors=True)


_create_template()
atexit.register(_cleanup)


class DBTestBase(unittest.TestCase):
    """SQLite and small local files without global config, Git, or workers."""

    def setUp(self) -> None:
        _restore_template()
        self.settings = _settings()
        self.config_values = build_config_values()
        self.db = DB(_DB_PATH, config_values=self.config_values)
        self.verification_task_store = VerificationTaskStore(self.db)
        self.workspace_service = WorkspaceService(
            self.db,
            self.settings,
            verification_task_store=self.verification_task_store,
        )
        self.fs_manager = FsManager(
            self.settings.cache_root,
            self.settings.artifacts_root,
        )
        self.runtime_blob_store = RuntimeBlobStore(self.fs_manager.runtime_root)
        self.runtime_cache_index = RuntimeCacheIndex(self.runtime_blob_store)
        self.user = f"alice-{uuid.uuid4().hex[:8]}"
        self.problem = f"{self.user}/sample-{uuid.uuid4().hex[:8]}"
