from __future__ import annotations

import unittest

from app.service.judgehost.host_telemetry import HostTelemetryStore


class TestHostTelemetryStore(unittest.TestCase):
    def test_normalizes_batches_before_aggregating_a_job(self) -> None:
        store = HostTelemetryStore()
        store.record_batch_leased("host-a", 1, [11, 12], leased_monotonic=10.0)
        store.record_case_reported(
            "host-a",
            1,
            11,
            reported_at="2026-08-03T01:00:04+00:00",
            reported_monotonic=14.0,
        )
        store.record_case_reported(
            "host-a",
            1,
            12,
            reported_at="2026-08-03T01:00:01+00:00",
            reported_monotonic=11.0,
        )
        self.assertEqual(
            store.snapshot()["host-a"]["last_judging_at"],
            "2026-08-03T01:00:04+00:00",
        )
        store.record_batch_leased("host-a", 1, [13], leased_monotonic=20.0)
        store.record_case_reported(
            "host-a",
            1,
            13,
            reported_at="2026-08-03T01:00:07+00:00",
            reported_monotonic=23.0,
        )
        store.record_job_terminal(1)

        row = store.snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 3)
        self.assertEqual(row["last_judging_at"], "2026-08-03T01:00:07+00:00")
        self.assertAlmostEqual(row["recent_avg_per_case_sec"] or 0.0, 7.0 / 3.0)

    def test_shared_job_keeps_host_samples_independent(self) -> None:
        store = HostTelemetryStore()
        store.record_batch_leased("host-a", 7, [1, 2], leased_monotonic=0.0)
        store.record_batch_leased("host-b", 7, [3, 4, 5], leased_monotonic=0.0)
        for case_id in [1, 2]:
            store.record_case_reported(
                "host-a",
                7,
                case_id,
                reported_at="2026-08-03T01:00:04+00:00",
                reported_monotonic=4.0,
            )
        for case_id in [3, 4, 5]:
            store.record_case_reported(
                "host-b",
                7,
                case_id,
                reported_at="2026-08-03T01:00:09+00:00",
                reported_monotonic=9.0,
            )
        store.record_job_terminal(7)

        rows = store.snapshot()
        self.assertEqual(rows["host-a"]["recent_avg_per_case_sec"], 2.0)
        self.assertEqual(rows["host-b"]["recent_avg_per_case_sec"], 3.0)

    def test_uses_the_median_of_only_the_last_ten_jobs(self) -> None:
        store = HostTelemetryStore()
        for job_id in range(1, 12):
            store.record_batch_leased("host-a", job_id, [job_id], leased_monotonic=0.0)
            store.record_case_reported(
                "host-a",
                job_id,
                job_id,
                reported_at=f"2026-08-03T01:00:{job_id:02d}+00:00",
                reported_monotonic=float(job_id),
            )
            store.record_job_terminal(job_id)

        row = store.snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 11)
        self.assertEqual(row["recent_avg_per_case_sec"], 6.5)

    def test_discards_incomplete_batches_without_losing_valid_counts(self) -> None:
        store = HostTelemetryStore()
        store.record_batch_leased("host-a", 1, [1, 2], leased_monotonic=0.0)
        store.record_case_reported(
            "host-a",
            1,
            1,
            reported_at="2026-08-03T01:00:01+00:00",
            reported_monotonic=1.0,
        )
        store.release_host("host-a")
        store.record_job_terminal(1)

        store.record_batch_leased("host-a", 2, [3], leased_monotonic=2.0)
        store.record_case_reported(
            "host-a",
            2,
            3,
            reported_at="2026-08-03T01:00:05+00:00",
            reported_monotonic=5.0,
        )
        store.record_job_terminal(2)
        store.record_job_terminal(2)

        row = store.snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 2)
        self.assertEqual(row["recent_avg_per_case_sec"], 3.0)


if __name__ == "__main__":
    unittest.main()
