from __future__ import annotations

import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.cleanup import JudgehostTerminalCleanup
from app.service.judgehost.task_store import JudgehostTaskStore
from app.service.platform.judge_fs_index import JudgeFsIndexService


def _task_row(
    index: int,
    *,
    verification_id: str = "verification",
    priority: str = "solution-run",
    group_key: str = "",
) -> dict[str, object]:
    now_text = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"task-{index}",
        "run_id": f"run-{index}",
        "problem_slug": "owner/problem",
        "username": "owner",
        "artifact_verification_id": verification_id,
        "mode": "pass-fail",
        "verification_id": verification_id,
        "status": "queued",
        "payload": {
            "task_kind": priority,
            "verification_source": "compile.only" if priority == "compile-only" else "run.execute",
            "compile_only": priority == "compile-only",
            "domjudge_group_key": group_key,
        },
        "result": {},
        "persist_verification_run": False,
        "error_text": "",
        "lease_owner": "",
        "lease_expires_at": "",
        "created_at": now_text,
        "updated_at": now_text,
        "completed_at": "",
        "attempt_count": 0,
        "summary": {},
        "enqueue_fingerprint": f"fingerprint-{index}",
    }


class TestJudgehostTaskScheduler(unittest.TestCase):
    def test_priority_heap_leases_compile_before_solution(self) -> None:
        store = JudgehostTaskStore()
        store.insert(_task_row(1))
        store.insert(_task_row(2, priority="compile-only"))

        lease = store.claim_ready(
            hostname="host-a",
            lease_until=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            now_text=datetime.now(timezone.utc).isoformat(),
        )

        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease["task_id"], "task-2")

    def test_concurrent_hosts_lease_each_task_once(self) -> None:
        store = JudgehostTaskStore()
        task_count = 128
        for index in range(task_count):
            store.insert(_task_row(index))
        leased: list[str] = []
        leased_lock = threading.Lock()
        barrier = threading.Barrier(16)
        lease_until = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

        def _worker(worker_id: int) -> None:
            barrier.wait(timeout=5)
            while True:
                lease = store.claim_ready(
                    hostname=f"host-{worker_id}",
                    lease_until=lease_until,
                    now_text=datetime.now(timezone.utc).isoformat(),
                )
                if lease is None:
                    return
                with leased_lock:
                    leased.append(lease["task_id"])

        threads = [threading.Thread(target=_worker, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(leased), task_count)
        self.assertEqual(len(set(leased)), task_count)
        self.assertEqual(store.status_counts()["queued"], 0)
        self.assertEqual(store.status_counts()["leased"], task_count)

    def test_group_claim_uses_queued_count_without_scanning_members(self) -> None:
        store = JudgehostTaskStore()
        for index in range(64):
            store.insert(_task_row(index, group_key="shared"))

        class _NoIterationSet(set[str]):
            def __iter__(self):
                raise AssertionError("group members were scanned")

        store._tasks_by_group["shared"] = _NoIterationSet(store._tasks_by_group["shared"])
        lease = store.claim_ready(
            hostname="host-a",
            lease_until=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            now_text=datetime.now(timezone.utc).isoformat(),
            group_key="shared",
        )

        self.assertIsNotNone(lease)
        self.assertEqual(store._queued_counts_by_group["shared"], 63)

    def test_case_cache_metadata_hit_does_not_read_payload_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="judge-fs-index-") as temp_dir:
            cache = JudgeFsIndexService(Path(temp_dir))
            key_hash = "1" * 64
            signature = "2" * 64
            cache.put(
                kind=JudgeFsIndexService.KIND_CASE,
                key_hash=key_hash,
                signature=signature,
                value={"manifest": []},
                files={"program.out": b"large output"},
            )

            with patch.object(Path, "read_bytes", side_effect=AssertionError("payload was read")):
                entry = cache.get(
                    kind=JudgeFsIndexService.KIND_CASE,
                    key_hash=key_hash,
                    signature=signature,
                )

            self.assertIsNotNone(entry)

    def test_terminal_cleanup_removes_runtime_identity_but_not_cache(self) -> None:
        store = JudgehostTaskStore()
        row = _task_row(1, verification_id="verification-cleanup")
        row["status"] = "completed"
        store.insert(row)

        class _StateStore:
            def __init__(self) -> None:
                self.forgotten_runs: list[str] = []

            def forget_runs(self, run_ids: list[str]) -> int:
                self.forgotten_runs.extend(run_ids)
                return len(run_ids)

        state_store = _StateStore()
        cleanup = JudgehostTerminalCleanup(store, state_store)

        cleanup._generation_by_verification["verification-cleanup"] = 2
        self.assertTrue(cleanup._cleanup("verification-cleanup", expected_generation=1))
        self.assertIsNotNone(store.get("task-1"))
        self.assertTrue(cleanup._cleanup("verification-cleanup", expected_generation=2))
        self.assertEqual(state_store.forgotten_runs, ["run-1"])
        self.assertIsNone(store.get("task-1"))


if __name__ == "__main__":
    unittest.main()
