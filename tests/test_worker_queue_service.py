from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.service.platform.worker_queue import WorkerQueueService


class TestWorkerQueueService(unittest.TestCase):
    def test_submit_rejects_when_queue_is_full(self) -> None:
        service = WorkerQueueService(worker_count=1, queue_capacity=1, history_limit=64)
        started = threading.Event()
        release = threading.Event()

        def _slow_job() -> None:
            started.set()
            release.wait(3.0)

        try:
            first, first_queued, first_reason = service.submit(
                name="slow",
                fn=_slow_job,
                queue_name="test",
                job_type="run",
            )
            self.assertTrue(first_queued, msg=first_reason)
            self.assertTrue(started.wait(2.0))
            second, second_queued, second_reason = service.submit(
                name="queued",
                fn=lambda: None,
                queue_name="test",
                job_type="run",
            )
            self.assertTrue(second_queued, msg=second_reason)
            rejected, queued, reason = service.submit(
                name="overflow",
                fn=lambda: None,
                queue_name="test",
                job_type="run",
            )
            self.assertFalse(queued)
            self.assertEqual(reason, "queue_rejected_full")
            self.assertIsNotNone(rejected.exception())
            release.set()
            service.wait_for_futures([first, second], timeout_sec=5.0)
            snapshot = service.snapshot(limit=20)
            rejected_jobs = [job for job in snapshot.get("jobs") or [] if str(job.get("status") or "") == "rejected"]
            self.assertTrue(rejected_jobs)
            self.assertTrue(any((str(job.get("error_code") or "") == "queue_rejected" for job in rejected_jobs)))
        finally:
            release.set()
            service.stop()

    def test_durable_log_recovers_inflight_jobs_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            durable_log = Path(tmpdir) / "worker-queue-events.jsonl"
            durable_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "job_created",
                                "job_id": "wq-old",
                                "name": "old-job",
                                "job_type": "preview",
                                "queue_name": "preview",
                                "created_at": 100.0,
                                "ts": 100.0,
                            }
                        ),
                        json.dumps(
                            {
                                "event": "job_started",
                                "job_id": "wq-old",
                                "started_at": 101.0,
                                "ts": 101.0,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            service = WorkerQueueService(
                worker_count=1,
                history_limit=64,
                durable_log_path=durable_log,
            )
            try:
                snapshot = service.snapshot(limit=20)
                rows = [job for job in snapshot.get("jobs") or [] if str(job.get("id") or "") == "wq-old"]
                self.assertTrue(rows)
                row = rows[0]
                self.assertEqual(str(row.get("status") or ""), "cancelled")
                self.assertEqual(str(row.get("error_code") or ""), "worker_cancelled")
                stats = snapshot.get("job_type_stats") or {}
                self.assertIn("preview", stats)
                self.assertGreaterEqual(int((stats.get("preview") or {}).get("cancelled") or 0), 1)
            finally:
                service.stop()

    def test_snapshot_reports_failure_codes_by_job_type(self) -> None:
        service = WorkerQueueService(worker_count=1, queue_capacity=8, history_limit=64)

        def _failing_compile_job() -> None:
            raise RuntimeError("compile failed: g++ error")

        try:
            worker, queued, reason = service.submit(
                name="compile-fail",
                fn=_failing_compile_job,
                queue_name="verification",
                job_type="verification",
            )
            self.assertTrue(queued, msg=reason)
            service.wait_for_futures([worker], timeout_sec=5.0)
            snapshot = service.snapshot(limit=20)
            rows = [job for job in snapshot.get("jobs") or [] if str(job.get("id") or "") == worker.job_id]
            self.assertTrue(rows)
            row = rows[0]
            self.assertEqual(str(row.get("status") or ""), "failed")
            self.assertEqual(str(row.get("error_code") or ""), "compile_error")
            stats = snapshot.get("job_type_stats") or {}
            verification_stats = stats.get("verification") or {}
            top_codes = verification_stats.get("top_failure_codes") or []
            self.assertTrue(top_codes)
            self.assertEqual(str(top_codes[0].get("code") or ""), "compile_error")
        finally:
            service.stop()

