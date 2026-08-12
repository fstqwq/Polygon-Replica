from __future__ import annotations

import sqlite3
from unittest.mock import patch

from app.impl.runtime.lifecycle import (
    _startup_clear_all_caches,
    _startup_reset_runtime_state,
)
from app.impl.runtime.config import config

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import (
    clear_startup_recovery_abort_fault,
    install_startup_recovery_abort_fault,
)
from tests.db_helpers import db_fetch_one
from tests.identity_helpers import canonical_test_verification_id


class TestRuntimeStartupE2E(BackendE2ETestBase):
    def test_startup_clear_all_caches_wipes_cache_root_artifacts_and_runtime(self) -> None:
        artifact_file = config.storage_layout.cache_artifacts_root / "verifications" / "ver-test" / "logs" / "compile.log"
        runtime_file = config.storage_layout.runtime_root / "blobs" / "aa" / ("a" * 64)
        durable_log = config.storage_layout.runtime_root / "worker-queue-events.jsonl"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        durable_log.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_text("{}", encoding="utf-8")
        runtime_file.write_text("ok\n", encoding="utf-8")
        durable_log.write_text("event\n", encoding="utf-8")

        with patch.object(config.runtime_cache_index, "clear_all", return_value=None), patch.object(
            config.worker_queue_service,
            "reset_runtime_history",
            return_value=None,
        ) as reset_history:
            _startup_clear_all_caches()

        reset_history.assert_called_once_with()

        self.assertTrue(config.storage_layout.cache_artifacts_root.exists())
        self.assertTrue(config.storage_layout.runtime_root.exists())
        self.assertFalse(artifact_file.exists())
        self.assertFalse(runtime_file.exists())
        self.assertFalse(durable_log.exists())

    def test_startup_recovery_failure_preserves_runtime_storage(self) -> None:
        context = config.workspace_service.workspace_context(
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
        marker = config.storage_layout.runtime_root / "startup-recovery-marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("must survive\n", encoding="utf-8")

        install_startup_recovery_abort_fault()
        try:
            with patch(
                "app.impl.runtime.lifecycle._startup_fail_summary_rows"
            ), patch(
                "app.impl.runtime.lifecycle._startup_cancel_judgehost_inflight"
            ) as cancel_judgehost, patch(
                "app.impl.runtime.lifecycle._startup_clear_all_caches",
                wraps=_startup_clear_all_caches,
            ) as clear_caches:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "forced startup recovery failure",
                ):
                    _startup_reset_runtime_state()
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
