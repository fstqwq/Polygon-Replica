from dataclasses import dataclass
from datetime import datetime, timezone

from app.db import now_iso
from app.service.judgehost.batch.model import HostLeaseRelease
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.configuration import JudgehostConfiguration
from app.service.judgehost.host.registry import JudgehostHostRegistry
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.retention import compact_payload_for_retention
from app.service.judgehost.task.time import parse_iso_utc


@dataclass(frozen=True)
class LeaseReconciliationOutcome:
    released_task_ids: tuple[str, ...]
    releases: tuple[HostLeaseRelease, ...]


class JudgehostMaintenance:
    """Apply global cancellation and retention operations across runtime owners."""

    def __init__(
        self,
        tasks: JudgehostTaskRegistry,
        batch_runtime: JudgehostBatchRuntime,
        hosts: JudgehostHostRegistry,
        configuration: JudgehostConfiguration,
    ) -> None:
        self._tasks = tasks
        self._batch_runtime = batch_runtime
        self._hosts = hosts
        self._configuration = configuration

    def cancel_unbatched_verification_tasks(self, verification_id: str, *, reason: str) -> int:
        affected = 0
        now_text = now_iso()
        for row in self._tasks.snapshots():
            if row["verification_id"] != verification_id:
                continue
            task_id = row["id"]
            if self._batch_runtime.batch_for_task(task_id) is not None:
                continue
            updated = self._tasks.transition(
                task_id,
                expected={"enqueuing", "queued", "leased"},
                status="failed",
                updates={
                    "payload": compact_payload_for_retention(row["payload"]),
                    "result": {"cancelled": True, "reason": reason, "error": reason},
                    "error_text": reason,
                    "updated_at": now_text,
                    "completed_at": now_text,
                },
            )
            affected += int(updated is not None)
        return affected

    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[dict[str, str]]:
        reason = reason.strip()
        if not reason:
            raise RuntimeError("judgehost startup cancel reason is required")
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        now_text = now_iso()
        for row in self._tasks.snapshots():
            if row["status"] not in {"enqueuing", "queued", "leased"}:
                continue
            key = (row["verification_id"], row["run_id"])
            if all(key) and key not in seen:
                seen.add(key)
                entries.append({"run_id": key[1], "verification_id": key[0]})
            if self._batch_runtime.batch_for_task(row["id"]) is not None:
                continue
            self._tasks.transition(
                row["id"],
                expected={"enqueuing", "queued", "leased"},
                status="failed",
                updates={
                    "payload": compact_payload_for_retention(row["payload"]),
                    "result": {"cancelled": True, "reason": reason, "error": reason},
                    "error_text": reason,
                    "updated_at": now_text,
                    "completed_at": now_text,
                },
            )
        return entries

    def forget_problem_tasks(self, problem_slug: str) -> int:
        return 0 if not problem_slug else self._tasks.remove_problem(problem_slug)

    def cancel_all_batches(self) -> list[int]:
        return self._batch_runtime.cancel_all_inflight(now_text=now_iso())

    def forget_runs(self, run_ids: list[str]) -> int:
        return self._batch_runtime.forget_runs(run_ids)

    def reconcile_expired_leases(self, verification_id: str) -> LeaseReconciliationOutcome:
        if not verification_id:
            return LeaseReconciliationOutcome((), ())
        now = datetime.now(timezone.utc)
        online_window = self._configuration.snapshot().online_window_sec
        stale_hosts = tuple(
            row["hostname"]
            for row in self._hosts.host_rows()
            if (seen_at := parse_iso_utc(row.get("last_seen_at"))) is not None
            and (now - seen_at).total_seconds() > online_window
        )
        released_task_ids: dict[str, None] = {}
        releases: list[HostLeaseRelease] = []
        for hostname in stale_hosts:
            selected_task_ids: dict[str, None] = {}
            for case in self._batch_runtime.cases_for_host(hostname):
                if case["status"] != "leased":
                    continue
                batch = self._batch_runtime.fetch_batch(case["batch_id"])
                if batch is None or batch["verification_id"] != verification_id:
                    continue
                if case["task_id"]:
                    selected_task_ids[case["task_id"]] = None
            if not selected_task_ids:
                continue
            release = self._batch_runtime.release_host_leases(
                hostname,
                now_text=now_iso(),
                verification_id=verification_id,
            )
            releases.append(release)
            released_task_ids.update(selected_task_ids)
        return LeaseReconciliationOutcome(
            released_task_ids=tuple(sorted(released_task_ids)),
            releases=tuple(releases),
        )
