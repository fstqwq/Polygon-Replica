from __future__ import annotations

import atexit
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path


_TESTSUITE_BASE = Path("./var")
_TESTSUITE_ROOT = _TESTSUITE_BASE / f"testsuite-{uuid.uuid4().hex[:8]}"


def _rmtree_retry(path: Path, attempts: int = 3, delay_sec: float = 0.1) -> None:
    target = Path(path)
    for _ in range(max(1, int(attempts))):
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            return
        time.sleep(max(0.0, float(delay_sec)))
    shutil.rmtree(target, ignore_errors=True)


def _cleanup_stale_testsuite_roots(exclude: Path | None = None) -> None:
    base = _TESTSUITE_BASE
    if not base.exists():
        return
    exclude_resolved = exclude.resolve() if exclude is not None else None
    for path in base.glob("testsuite-*"):
        if not path.is_dir():
            continue
        if exclude_resolved is not None:
            try:
                if path.resolve() == exclude_resolved:
                    continue
            except Exception:
                pass
        _rmtree_retry(path)


def testsuite_root() -> Path:
    return _TESTSUITE_ROOT


def _cleanup_testsuite_root() -> None:
    _rmtree_retry(testsuite_root())


atexit.register(_cleanup_testsuite_root)
_cleanup_stale_testsuite_roots(exclude=testsuite_root())


def ensure_local_env() -> None:
    root = testsuite_root()
    os.environ["POLYGONLIKE_DB"] = str(root / "polygonlike.db")
    os.environ["POLYGONLIKE_BARE_ROOT"] = str(root / "srv" / "git")
    os.environ["POLYGONLIKE_WORKSPACE_ROOT"] = str(root / "srv" / "workspaces")
    os.environ["POLYGONLIKE_RUN_ROOT"] = str(root / "srv" / "runs")
    os.environ["POLYGONLIKE_ARTIFACTS_ROOT"] = str(root / "lib" / "polygonlike" / "artifacts")
    os.environ["POLYGONLIKE_CACHE_ROOT"] = str(root / "cache" / "polygonlike")
    os.environ["POLYGONLIKE_AUTH_COOKIE_SECURE"] = "1"


ensure_local_env()

from app.impl.config import config  # noqa: E402
from app.impl.workspace import _wait_for_export_workers, _wait_for_preview_workers, _wait_for_run_execute_workers, _wait_for_verification_workers  # noqa: E402

build_service = config.build_service
db = config.db
export_service = config.export_service
preview_service = config.preview_service
run_service = config.run_service
workspace_service = config.workspace_service


class SmokeBase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            _wait_for_run_execute_workers(timeout_sec=10.0)
        except Exception:
            pass
        try:
            _wait_for_verification_workers(timeout_sec=10.0)
        except Exception:
            pass
        try:
            _wait_for_preview_workers(timeout_sec=10.0)
        except Exception:
            pass
        try:
            _wait_for_export_workers(timeout_sec=10.0)
        except Exception:
            pass
        with workspace_service._cache_lock:
            workspace_service._problem_cache.clear()
            workspace_service._user_cache.clear()
        _cleanup_stale_testsuite_roots(exclude=testsuite_root())
        _cleanup_testsuite_root()
        self.addCleanup(_cleanup_testsuite_root)
        db.init()
        self.test_id = uuid.uuid4().hex[:8]
        self.problem = self.random_id("sample")
        self.user = self.random_id("alice")

        self._seed_workspace(self.problem, self.user, "Sample Problem")
        self._seed_workspace("sample", "alice", "Sample Problem")

    def _seed_workspace(self, problem: str, user: str, problem_name: str) -> Path:
        workspace_service.ensure_problem(problem, problem_name)
        ws = Path(workspace_service.ensure_workspace(problem, user))
        workspace_service.grant_repo_access(problem, user, "owner")
        for rel in [
            "statement",
            "config",
            "validators",
            "checkers",
            "interactors",
            "generators",
            "solutions",
            "tests/manual",
            "tests/generator",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)
        testlib = ws / "third_party/testlib/testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")
        return ws

    def random_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def _artifact_root(self, artifact_id: str) -> Path:
        problem = str(getattr(self, "problem", "sample"))
        return Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / artifact_id

    def _workspace_path(self) -> Path:
        problem = str(getattr(self, "problem", "sample"))
        user = str(getattr(self, "user", "alice"))
        ctx = workspace_service.workspace_context(problem, user, include_recent=False)
        return Path(str(ctx["workspace"]["path"]))
