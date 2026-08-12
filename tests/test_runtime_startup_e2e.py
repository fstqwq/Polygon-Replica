import sqlite3
from unittest.mock import patch

from app.runtime_lifecycle import (
    _startup_clear_all_caches,
    _startup_reset_runtime_state,
)
from app.main import runtime

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

        with patch.object(runtime.runtime_cache_index, "clear_all", return_value=None), patch.object(
            runtime.worker_queue_service,
            "reset_runtime_history",
            return_value=None,
        ) as reset_history:
            _startup_clear_all_caches(runtime)

        reset_history.assert_called_once_with()

        self.assertTrue(runtime.storage_layout.cache_root.exists())
        self.assertFalse(artifact_file.exists())
        self.assertFalse(runtime_file.exists())
        self.assertFalse(durable_log.exists())
        self.assertFalse(upload_file.exists())
        self.assertFalse(contest_draft.exists())

    def test_startup_cache_clear_failure_is_fatal(self) -> None:
        with patch.object(
            runtime.worker_queue_service,
            "reset_runtime_history",
            return_value=None,
        ), patch.object(
            runtime.runtime_cache_index,
            "clear_all",
            side_effect=RuntimeError("cache is busy"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cache is busy"):
                _startup_clear_all_caches(runtime)

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
            with patch(
                "app.runtime_lifecycle._startup_fail_summary_rows"
            ), patch(
                "app.runtime_lifecycle._startup_cancel_judgehost_inflight"
            ) as cancel_judgehost, patch(
                "app.runtime_lifecycle._startup_clear_all_caches",
                wraps=_startup_clear_all_caches,
            ) as clear_caches:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "forced startup recovery failure",
                ):
                    _startup_reset_runtime_state(runtime)
        finally:
            clear_startup_recovery_abort_fault()

        cancel_judgehost.assert_not_called()
        clear_caches.assert_not_called()
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
