from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from tests.common import SmokeBase
from app.impl.config import config
from app.services.runtime_cache_service import RuntimeCacheService

db = config.db
workspace_service = config.workspace_service


class TestRuntimeCacheCleanup(SmokeBase):
    def _set_tree_mtime(self, root: Path, ts: float) -> None:
        if root.is_file():
            os.utime(root, (ts, ts))
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            base = Path(dirpath)
            for name in filenames:
                p = base / name
                try:
                    os.utime(p, (ts, ts))
                except OSError:
                    pass
            for name in dirnames:
                p = base / name
                try:
                    os.utime(p, (ts, ts))
                except OSError:
                    pass
            try:
                os.utime(base, (ts, ts))
            except OSError:
                pass

    def test_cleanup_prunes_runtime_cache_but_keeps_export_zip(self) -> None:
        old_env = {
            "POLYGONLIKE_ARTIFACT_CACHE_CLEANUP_INTERVAL_SEC": os.environ.get("POLYGONLIKE_ARTIFACT_CACHE_CLEANUP_INTERVAL_SEC"),
            "POLYGONLIKE_ARTIFACT_CACHE_TTL_SEC": os.environ.get("POLYGONLIKE_ARTIFACT_CACHE_TTL_SEC"),
            "POLYGONLIKE_RUN_CACHE_TTL_SEC": os.environ.get("POLYGONLIKE_RUN_CACHE_TTL_SEC"),
        }
        os.environ["POLYGONLIKE_ARTIFACT_CACHE_CLEANUP_INTERVAL_SEC"] = "0"
        os.environ["POLYGONLIKE_ARTIFACT_CACHE_TTL_SEC"] = "1"
        os.environ["POLYGONLIKE_RUN_CACHE_TTL_SEC"] = "1"
        try:
            ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
            problem_id = int(ctx["problem"]["id"])
            workspace_id = int(ctx["workspace"]["id"])
            artifact_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]).resolve()
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]).resolve()
            sample_root = artifact_root / self.problem
            sample_root.mkdir(parents=True, exist_ok=True)
            run_root.mkdir(parents=True, exist_ok=True)

            token = uuid.uuid4().hex[:8]
            exported_build_id = f"b-cache-exp-{token}"
            cached_build_id = f"b-cache-old-{token}"
            cached_preview_id = f"p-cache-old-{token}"
            fresh_build_id = f"b-cache-fresh-{token}"
            export_file = "package.zip"

            exported_dir = sample_root / exported_build_id
            (exported_dir / "tests").mkdir(parents=True, exist_ok=True)
            (exported_dir / "logs").mkdir(parents=True, exist_ok=True)
            (exported_dir / "export").mkdir(parents=True, exist_ok=True)
            (exported_dir / "tests" / "001.in").write_text("1\n", encoding="utf-8")
            (exported_dir / "logs" / "compile.log").write_text("log\n", encoding="utf-8")
            (exported_dir / "export" / export_file).write_bytes(b"zip-bytes")

            cached_build_dir = sample_root / cached_build_id
            (cached_build_dir / "tests").mkdir(parents=True, exist_ok=True)
            (cached_build_dir / "tests" / "001.in").write_text("2\n", encoding="utf-8")

            cached_preview_dir = sample_root / cached_preview_id
            (cached_preview_dir / "statement_preview").mkdir(parents=True, exist_ok=True)
            (cached_preview_dir / "statement_preview" / "statement.pdf").write_bytes(b"%PDF")

            fresh_build_dir = sample_root / fresh_build_id
            (fresh_build_dir / "tests").mkdir(parents=True, exist_ok=True)
            (fresh_build_dir / "tests" / "001.in").write_text("3\n", encoding="utf-8")

            old_run = run_root / f"run-old-{token}"
            old_run.mkdir(parents=True, exist_ok=True)
            (old_run / "summary.json").write_text("{}", encoding="utf-8")
            old_invalid = run_root / "invalid-runs" / f"run-old-invalid-{token}"
            old_invalid.mkdir(parents=True, exist_ok=True)
            (old_invalid / "summary.json").write_text("{}", encoding="utf-8")
            fresh_run = run_root / f"run-fresh-{token}"
            fresh_run.mkdir(parents=True, exist_ok=True)
            (fresh_run / "summary.json").write_text("{}", encoding="utf-8")

            now_iso = "2026-02-23T00:00:00+00:00"
            db.execute(
                """
                INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    exported_build_id,
                    problem_id,
                    workspace_id,
                    "",
                    "main",
                    "ok",
                    "{}",
                    str(exported_dir),
                    now_iso,
                    now_iso,
                ],
            )
            db.execute(
                """
                INSERT INTO exports(id,problem_id,build_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    f"e-{token}",
                    problem_id,
                    exported_build_id,
                    workspace_id,
                    "icpc",
                    export_file,
                    "0" * 64,
                    9,
                    "",
                    now_iso,
                ],
            )

            old_ts = time.time() - 3600
            for p in [exported_dir, cached_build_dir, cached_preview_dir, old_run, old_invalid]:
                self._set_tree_mtime(p, old_ts)

            cleaner = RuntimeCacheService(db, artifact_root, run_root)
            ran = cleaner.cleanup_cache(force=True)
            self.assertTrue(ran)

            self.assertTrue(exported_dir.exists())
            self.assertTrue((exported_dir / "export" / export_file).exists())
            self.assertFalse((exported_dir / "tests").exists())
            self.assertFalse((exported_dir / "logs").exists())

            self.assertFalse(cached_build_dir.exists())
            self.assertFalse(cached_preview_dir.exists())
            self.assertTrue(fresh_build_dir.exists())

            self.assertFalse(old_run.exists())
            self.assertFalse(old_invalid.exists())
            self.assertTrue(fresh_run.exists())
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
