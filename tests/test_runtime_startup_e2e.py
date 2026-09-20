import sqlite3

from app.runtime_lifecycle import (
    _startup_clear_all_caches,
    _startup_reset_runtime_state,
)
from app.main import runtime
from app.service.platform.runtime_cache_index import RuntimeCacheIndex

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import (
    clear_startup_recovery_abort_fault,
    install_startup_recovery_abort_fault,
)
from tests.db_helpers import db_fetch_one
from tests.identity_helpers import canonical_test_verification_id


class TestRuntimeStartupE2E(BackendE2ETestBase):
    def test_startup_clear_all_caches_wipes_entire_cache_root(self) -> None:
        artifact_file = runtime.storage_layout.cache_artifacts_root / "verifications" / "ver-test" / "logs" / "compile.log"
        runtime_file = runtime.storage_layout.runtime_root / "blobs" / "aa" / ("a" * 64)
        durable_log = runtime.storage_layout.runtime_root / "worker-queue-events.jsonl"
        upload_file = runtime.storage_layout.archive_upload_root / "upload.zip"
        contest_draft = runtime.storage_layout.contest_import_draft_root / "draft.zip"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        durable_log.parent.mkdir(parents=True, exist_ok=True)
        upload_file.parent.mkdir(parents=True, exist_ok=True)
        contest_draft.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_text("{}", encoding="utf-8")
        runtime_file.write_text("ok\n", encoding="utf-8")
        durable_log.write_text("event\n", encoding="utf-8")
        upload_file.write_bytes(b"upload")
        contest_draft.write_bytes(b"draft")

        entry = runtime.runtime_cache_index.put(
            namespace=RuntimeCacheIndex.EXECUTABLE,
            key_hash="a" * 64,
            signature="b" * 64,
            value={"executable": "fixture"},
            files={"program": b"executable"},
        )
        future, _accepted, _reason = runtime.worker_queue_service.submit(
            name="startup-history", fn=lambda: None,
        )
        future.join(2)
        self.assertFalse(future.is_alive())
        self.assertIsNone(future.exception())
        _startup_clear_all_caches(runtime)

        self.assertTrue(runtime.storage_layout.cache_root.exists())
        self.assertIsNone(runtime.runtime_cache_index.get(
            namespace=entry.namespace, key_hash=entry.key_hash, signature=entry.signature,
        ))
        self.assertEqual(runtime.worker_queue_service.snapshot()["jobs"], [])
        self.assertFalse(artifact_file.exists())
        self.assertFalse(runtime_file.exists())
        self.assertFalse(durable_log.exists())
        self.assertFalse(upload_file.exists())
        self.assertFalse(contest_draft.exists())

    def test_startup_cache_clear_failure_is_fatal(self) -> None:
        marker = runtime.storage_layout.runtime_root / "active-cache-marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("active cache\n", encoding="utf-8")
        with runtime.runtime_cache_index.key_lock(
            RuntimeCacheIndex.EXECUTABLE, "a" * 64, "b" * 64,
        ):
            with self.assertRaisesRegex(RuntimeError, "entries are active"):
                _startup_clear_all_caches(runtime)
        self.assertEqual(marker.read_text(encoding="utf-8"), "active cache\n")

    def test_startup_recovery_failure_preserves_runtime_storage(self) -> None:
        context = runtime.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        verification_id = canonical_test_verification_id(
            f"startup-recovery-failure:{self.test_id}"
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=int(context["problem"]["id"]),
            workspace_id=int(context["workspace"]["id"]),
        )
        marker = runtime.storage_layout.runtime_root / "startup-recovery-marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("must survive\n", encoding="utf-8")

        install_startup_recovery_abort_fault()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced startup recovery failure",
            ):
                _startup_reset_runtime_state(runtime)
        finally:
            clear_startup_recovery_abort_fault()

        self.assertTrue(marker.exists())
        verification = db_fetch_one(
            "SELECT status FROM verifications WHERE id=?",
            [verification_id],
        )
        task = db_fetch_one(
            "SELECT final_status FROM verification_tasks WHERE id=?",
            [task_id],
        )
        self.assertIsNotNone(verification)
        self.assertIsNotNone(task)
        self.assertEqual(str(verification["status"]), "running")
        self.assertEqual(str(task["final_status"]), "")
