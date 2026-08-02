from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TypedDict


class HostTelemetryRow(TypedDict):
    judged_case_count: int
    last_judging_at: str | None
    recent_avg_per_case_sec: float | None


@dataclass(slots=True)
class _LeaseBatch:
    job_id: int
    pending_case_ids: set[int]
    case_count: int
    leased_monotonic: float
    latest_reported_monotonic: float


@dataclass(slots=True)
class _JobTiming:
    busy_sec: float = 0.0
    case_count: int = 0


@dataclass(slots=True)
class _HostTelemetry:
    judged_case_count: int = 0
    last_judging_at: str | None = None
    last_judging_monotonic: float | None = None
    recent_job_avg_sec: deque[float] = field(default_factory=lambda: deque(maxlen=10))
    recent_avg_per_case_sec: float | None = None
    active_batch: _LeaseBatch | None = None
    job_timings: dict[int, _JobTiming] = field(default_factory=dict)


class HostTelemetryStore:
    """Keep bounded, runtime-only judgehost throughput telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, _HostTelemetry] = {}
        self._hosts_by_job: dict[int, set[str]] = {}

    def _host(self, hostname: str) -> _HostTelemetry:
        telemetry = self._hosts.get(hostname)
        if telemetry is None:
            telemetry = _HostTelemetry()
            self._hosts[hostname] = telemetry
        return telemetry

    def _unindex_unused_job(self, hostname: str, telemetry: _HostTelemetry, job_id: int) -> None:
        active = telemetry.active_batch
        if job_id in telemetry.job_timings or (active is not None and active.job_id == job_id):
            return
        hosts = self._hosts_by_job.get(job_id)
        if hosts is None:
            return
        hosts.discard(hostname)
        if not hosts:
            self._hosts_by_job.pop(job_id, None)

    def _drop_active_batch(self, hostname: str, telemetry: _HostTelemetry) -> None:
        batch = telemetry.active_batch
        if batch is None:
            return
        telemetry.active_batch = None
        self._unindex_unused_job(hostname, telemetry, batch.job_id)

    def record_batch_leased(
        self,
        hostname: str,
        job_id: int,
        case_ids: list[int],
        *,
        leased_monotonic: float,
    ) -> None:
        pending_case_ids = {int(case_id) for case_id in case_ids}
        if not pending_case_ids:
            return
        safe_job_id = int(job_id)
        with self._lock:
            telemetry = self._host(hostname)
            # A host cannot normally lease a second batch while owning Cases.
            # Dropping stale timing state keeps cancellation/requeue recovery safe.
            self._drop_active_batch(hostname, telemetry)
            telemetry.active_batch = _LeaseBatch(
                job_id=safe_job_id,
                pending_case_ids=pending_case_ids,
                case_count=len(pending_case_ids),
                leased_monotonic=float(leased_monotonic),
                latest_reported_monotonic=float(leased_monotonic),
            )
            self._hosts_by_job.setdefault(safe_job_id, set()).add(hostname)

    def record_case_reported(
        self,
        hostname: str,
        job_id: int,
        case_id: int,
        *,
        reported_at: str,
        reported_monotonic: float,
    ) -> None:
        safe_job_id = int(job_id)
        safe_case_id = int(case_id)
        with self._lock:
            telemetry = self._host(hostname)
            telemetry.judged_case_count += 1
            safe_reported_monotonic = float(reported_monotonic)
            if (
                telemetry.last_judging_monotonic is None
                or safe_reported_monotonic >= telemetry.last_judging_monotonic
            ):
                telemetry.last_judging_at = reported_at
                telemetry.last_judging_monotonic = safe_reported_monotonic
            batch = telemetry.active_batch
            if (
                batch is None
                or batch.job_id != safe_job_id
                or safe_case_id not in batch.pending_case_ids
            ):
                return
            batch.pending_case_ids.remove(safe_case_id)
            batch.latest_reported_monotonic = max(
                batch.latest_reported_monotonic,
                safe_reported_monotonic,
            )
            if batch.pending_case_ids:
                return
            elapsed = max(0.0, batch.latest_reported_monotonic - batch.leased_monotonic)
            timing = telemetry.job_timings.setdefault(safe_job_id, _JobTiming())
            timing.busy_sec += elapsed
            timing.case_count += batch.case_count
            telemetry.active_batch = None

    def record_job_terminal(self, job_id: int) -> None:
        safe_job_id = int(job_id)
        with self._lock:
            hostnames = self._hosts_by_job.pop(safe_job_id, set())
            for hostname in hostnames:
                telemetry = self._hosts[hostname]
                batch = telemetry.active_batch
                if batch is not None and batch.job_id == safe_job_id:
                    telemetry.active_batch = None
                timing = telemetry.job_timings.pop(safe_job_id, None)
                if timing is None or timing.case_count <= 0:
                    continue
                telemetry.recent_job_avg_sec.append(timing.busy_sec / timing.case_count)
                telemetry.recent_avg_per_case_sec = float(
                    statistics.median(telemetry.recent_job_avg_sec)
                )

    def release_host(self, hostname: str) -> None:
        with self._lock:
            telemetry = self._hosts.get(hostname)
            if telemetry is not None:
                self._drop_active_batch(hostname, telemetry)

    def snapshot(self) -> dict[str, HostTelemetryRow]:
        with self._lock:
            return {
                hostname: {
                    "judged_case_count": telemetry.judged_case_count,
                    "last_judging_at": telemetry.last_judging_at,
                    "recent_avg_per_case_sec": telemetry.recent_avg_per_case_sec,
                }
                for hostname, telemetry in self._hosts.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._hosts_by_job.clear()
