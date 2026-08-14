"""Deterministic ready selection and leasing for Judgehost batches.

Hosts select an existing lease, foreground work, host affinity, a prerequisite,
and finally the global ready minimum. A policy receives immutable candidates;
BatchState validates and applies its decision under the canonical lock.
"""

import heapq
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.service.judgehost.batch.model import (
    CompileSubmission,
    ExecutionBatchRecord,
    ExecutionBatchRow,
    ExecutionBatchSpec,
    LeaseClaim,
    HostLeaseTelemetry,
    HostTelemetryRow,
    MaterializationClaim,
    HostTelemetryState,
    JudgehostCaseRow,
)
from app.service.judgehost.batch.policy import (
    SchedulingCandidate,
    SchedulingDecision,
    SchedulingSnapshot,
)
from app.service.judgehost.batch.snapshot import batch_snapshot, case_snapshot

if TYPE_CHECKING:
    from app.service.judgehost.batch.state import BatchState


class BatchDispatch:
    """Own ready selection, materialization claims, leases, and host telemetry."""

    def __init__(self, state: "BatchState") -> None:
        self._state = state

    def _peek_ready_batch_locked(self) -> ExecutionBatchRecord | None:
        entry = self._state._ready_batches.first()
        if entry is None:
            return None
        batch = self._state._batches.get(entry[-1])
        if batch is None or self._state._batch_heap_key_locked(batch) != entry:
            raise RuntimeError("ready Batch index is inconsistent")
        return batch

    def _dispatch_batch_locked(self, batch: ExecutionBatchRecord) -> None:
        batch.dispatch_count += 1
        self._state._touch_batch_locked(batch)

    def _take_ready_batch_locked(self) -> ExecutionBatchRecord | None:
        batch = self._peek_ready_batch_locked()
        if batch is None:
            return None
        self._dispatch_batch_locked(batch)
        return batch

    def _prune_host_context_locked(self, hostname: str) -> deque[int]:
        queue_ids = self._state._affinity_batches_by_host[hostname]
        retained = deque(
            batch_id
            for batch_id in queue_ids
            if (batch := self._state._batches.get(batch_id)) is not None
            and batch.status == "open"
        )
        if retained:
            self._state._affinity_batches_by_host[hostname] = retained
        else:
            self._state._affinity_batches_by_host.pop(hostname, None)
        return retained

    def _remember_host_batch_locked(
        self,
        hostname: str,
        batch: ExecutionBatchRecord,
    ) -> None:
        queue_ids = self._prune_host_context_locked(hostname)
        if batch.batch_id in queue_ids:
            return
        if len(queue_ids) < self._state._AFFINITY_QUEUE_SIZE:
            queue_ids.append(batch.batch_id)
            self._state._affinity_batches_by_host[hostname] = queue_ids

    def _ready_host_batch_locked(
        self,
        hostname: str,
        batch_ids: Iterable[int],
    ) -> ExecutionBatchRecord | None:
        for batch_id in batch_ids:
            batch = self._state._batches.get(batch_id)
            if (
                batch is not None
                and self._state._batch_next_case_locked(batch, hostname=hostname)
                is not None
            ):
                return batch
        return None

    def _peek_ready_prerequisite_locked(
        self,
        *,
        verification_id: str,
        task_kind: str,
        hostname: str,
    ) -> ExecutionBatchRecord | None:
        index_key = (verification_id, task_kind)
        index = self._state._ready_prerequisites.get(index_key)
        if not index:
            return None
        entry = index.first()
        if entry is None:
            return None
        batch = self._state._batches.get(entry[-1])
        if batch is None or self._state._batch_heap_key_locked(batch) != entry:
            raise RuntimeError("ready prerequisite index is inconsistent")
        return batch

    def _peek_unblocking_batch_locked(
        self,
        hostname: str,
        affinity_ids: Iterable[int],
    ) -> ExecutionBatchRecord | None:
        for batch_id in affinity_ids:
            blocked = self._state._batches.get(batch_id)
            if blocked is None or blocked.status != "open":
                continue
            task_kinds = (
                ("generate-input",)
                if blocked.task_kind == "main-correct"
                else (
                    self._state._PREREQUISITE_TASK_KINDS
                    if blocked.task_kind not in {"generate-input", "compile-only"}
                    else ()
                )
            )
            for task_kind in task_kinds:
                batch = self._peek_ready_prerequisite_locked(
                    verification_id=blocked.verification_id,
                    task_kind=task_kind,
                    hostname=hostname,
                )
                if batch is not None:
                    return batch
        return None

    def _scheduling_candidate_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        hostname: str,
    ) -> SchedulingCandidate | None:
        next_case = self._state._batch_next_case_locked(batch, hostname=hostname)
        if next_case is None:
            return None
        pending_case_ids = tuple(
            case.id
            for case in self._state._sorted_cases_locked(
                self._state._case_ids_by_batch[batch.batch_id]
            )
            if case.status == "pending"
        )
        active_hosts = frozenset(
            case.lease_owner
            for case in (
                self._state._cases[case_id]
                for case_id in self._state._case_ids_by_batch[batch.batch_id]
            )
            if case.status in {"leased", "reporting"} and case.lease_owner is not None
        )
        return SchedulingCandidate(
            batch_id=batch.batch_id,
            service_class=batch.service_class,
            scope_sequence=next_case.scope_sequence,
            pending_count=self._state._batch_counts[batch.batch_id].pending,
            dispatch_count=batch.dispatch_count,
            compile_key=batch.compile_key,
            compile_state=batch.compile_state,
            materialization_state=batch.materialization_state,
            active_hosts=active_hosts,
            pending_case_ids=pending_case_ids,
        )

    def _scheduling_snapshot_locked(self, hostname: str) -> SchedulingSnapshot:
        def blocked_snapshot() -> SchedulingSnapshot:
            return SchedulingSnapshot(
                hostname=hostname,
                leased_batch_id=None,
                foreground_batch_id=None,
                affinity_batch_ids=(),
                affinity_ready_batch_id=None,
                stolen_batch_id=None,
                prerequisite_batch_id=None,
                global_batch_id=None,
                candidates=(),
            )

        leased_batch_ids = {
            self._state._cases[case_id].batch_id
            for case_id in self._state._leased_case_ids_by_host.get(hostname, ())
            if case_id in self._state._cases
        }
        leased_batch_id = None
        if len(leased_batch_ids) > 1:
            return blocked_snapshot()
        if len(leased_batch_ids) == 1:
            candidate_id = next(iter(leased_batch_ids))
            batch = self._state._batches.get(candidate_id)
            if (
                batch is None
                or self._state._batch_next_case_locked(batch, hostname=hostname) is None
            ):
                return blocked_snapshot()
            leased_batch_id = candidate_id

        global_batch = self._peek_ready_batch_locked()
        foreground_batch_id = (
            global_batch.batch_id
            if global_batch is not None and global_batch.service_class == "foreground"
            else None
        )
        affinity_ids = tuple(self._prune_host_context_locked(hostname))
        affinity_batch = self._ready_host_batch_locked(hostname, affinity_ids)
        prerequisite = self._peek_unblocking_batch_locked(hostname, affinity_ids)
        stolen_batch_id = self._state._stolen_batch_by_host.get(hostname)
        if stolen_batch_id is not None:
            stolen = self._state._batches.get(stolen_batch_id)
            if (
                stolen is None
                or self._state._batch_next_case_locked(stolen, hostname=hostname)
                is None
            ):
                self._state._stolen_batch_by_host.pop(hostname, None)
                stolen_batch_id = None
        candidate_batch_ids = (
            set(self._state._batches)
            if self._state._scheduling_policy.catalog_all_candidates
            else {
                batch_id
                for batch_id in (
                    leased_batch_id,
                    foreground_batch_id,
                    None if affinity_batch is None else affinity_batch.batch_id,
                    None if prerequisite is None else prerequisite.batch_id,
                    None if global_batch is None else global_batch.batch_id,
                )
                if batch_id is not None
            }
        )
        candidates = tuple(
            candidate
            for batch in (
                self._state._batches[batch_id]
                for batch_id in sorted(candidate_batch_ids)
                if batch_id in self._state._batches
            )
            if (
                candidate := self._scheduling_candidate_locked(
                    batch,
                    hostname=hostname,
                )
            )
            is not None
        )
        return SchedulingSnapshot(
            hostname=hostname,
            leased_batch_id=leased_batch_id,
            foreground_batch_id=foreground_batch_id,
            affinity_batch_ids=affinity_ids,
            affinity_ready_batch_id=(
                None if affinity_batch is None else affinity_batch.batch_id
            ),
            stolen_batch_id=stolen_batch_id,
            prerequisite_batch_id=(
                None if prerequisite is None else prerequisite.batch_id
            ),
            global_batch_id=None if global_batch is None else global_batch.batch_id,
            candidates=candidates,
        )

    def _apply_scheduling_decision_locked(
        self,
        hostname: str,
        snapshot: SchedulingSnapshot,
        decision: SchedulingDecision | None,
    ) -> ExecutionBatchRecord | None:
        if decision is None:
            return None
        candidate_ids = {candidate.batch_id for candidate in snapshot.candidates}
        if decision.batch_id not in candidate_ids:
            return None
        batch = self._state._batches.get(decision.batch_id)
        if (
            batch is None
            or self._state._batch_next_case_locked(batch, hostname=hostname) is None
        ):
            return None
        if decision.increment_dispatch:
            self._dispatch_batch_locked(batch)
        if decision.remember:
            previous = tuple(self._prune_host_context_locked(hostname))
            self._remember_host_batch_locked(hostname, batch)
            current = tuple(self._prune_host_context_locked(hostname))
            if (
                decision.track_as_stolen
                and batch.batch_id not in current
                and batch.batch_id not in previous
            ):
                self._state._stolen_batch_by_host[hostname] = batch.batch_id
        return batch

    def host_context_batches(self, hostname: str) -> list[ExecutionBatchRow]:
        with self._state._lock:
            affinity_ids = self._prune_host_context_locked(hostname)
            return [
                batch_snapshot(self._state._batches[batch_id])
                for batch_id in affinity_ids
                if batch_id in self._state._batches
            ]

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        with self._state._lock:
            snapshot = self._scheduling_snapshot_locked(hostname)
            decision = self._state._scheduling_policy.select(snapshot)
            selected = self._apply_scheduling_decision_locked(
                hostname,
                snapshot,
                decision,
            )
            if selected is None:
                return None
            return batch_snapshot(selected)

    def wait_for_ready_batch(self, timeout_sec: float) -> bool:
        """Wait for one readiness transition without retaining the Scheduler lock."""
        with self._state._ready_condition:
            if self._state._ready_batches.first() is not None:
                return True
            generation = self._state._ready_generation
            self._state._ready_condition.wait_for(
                lambda: self._state._ready_generation != generation,
                timeout=max(0.0, float(timeout_sec)),
            )
            return self._state._ready_batches.first() is not None

    def host_leased_case_count(self, hostname: str) -> int:
        with self._state._lock:
            return len(self._state._leased_case_ids_by_host.get(hostname, ()))

    def batch_case_count(self, batch_id: int, *, status: str) -> int:
        """Return a case-state count without scanning a batch's cases."""
        with self._state._lock:
            counts = self._state._batch_counts.get(int(batch_id))
            return (
                0
                if counts is None
                else int(getattr(counts, self._state._status_attr(status)))
            )

    def batch_dispatch_count(self, batch_id: int) -> int:
        """Return the process-local number of non-affinity dispatches."""
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            return 0 if batch is None else batch.dispatch_count

    def active_lease_counts(self) -> dict[str, int]:
        with self._state._lock:
            return {
                hostname: len(case_ids)
                for hostname, case_ids in self._state._leased_case_ids_by_host.items()
                if case_ids
            }

    def cases_for_host(self, hostname: str) -> list[JudgehostCaseRow]:
        with self._state._lock:
            return [
                case_snapshot(self._state._cases[case_id])
                for case_id in self._state._leased_case_ids_by_host.get(hostname, ())
                if case_id in self._state._cases
            ]

    def record_batch_leased(
        self,
        hostname: str,
        batch_id: int,
        case_ids: list[int],
        *,
        leased_monotonic: float,
    ) -> None:
        pending_case_ids = {int(case_id) for case_id in case_ids}
        if not pending_case_ids:
            return
        with self._state._lock:
            self._state._drop_host_telemetry_batch_locked(hostname)
            telemetry = self._state._host_telemetry.setdefault(
                hostname, HostTelemetryState()
            )
            telemetry.active_batch = HostLeaseTelemetry(
                batch_id=int(batch_id),
                pending_case_ids=pending_case_ids,
                case_count=len(pending_case_ids),
                leased_monotonic=float(leased_monotonic),
                latest_reported_monotonic=float(leased_monotonic),
            )
            self._state._telemetry_hosts_by_batch[int(batch_id)].add(hostname)

    def host_telemetry_snapshot(self) -> dict[str, HostTelemetryRow]:
        with self._state._lock:
            return {
                hostname: {
                    "judged_case_count": telemetry.judged_case_count,
                    "last_judging_at": telemetry.last_judging_at,
                    "last_judging": (
                        None
                        if telemetry.last_judging is None
                        else {
                            "verification_id": telemetry.last_judging[
                                "verification_id"
                            ],
                            "problem_slug": telemetry.last_judging["problem_slug"],
                            "task_kind": telemetry.last_judging["task_kind"],
                            "source_label": telemetry.last_judging["source_label"],
                            "test_name": telemetry.last_judging["test_name"],
                        }
                    ),
                    "recent_avg_per_case_sec": telemetry.recent_avg_per_case_sec,
                }
                for hostname, telemetry in self._state._host_telemetry.items()
            }

    def batch_spec(self, batch_id: int) -> ExecutionBatchSpec | None:
        with self._state._lock:
            return self._state._batch_specs.get(int(batch_id))

    def compile_submission_for_batch(self, batch_id: int) -> CompileSubmission | None:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if batch is None:
                return None
            return self._state._compile_submissions_by_key.get(batch.compile_key)

    def claim_materialization(
        self,
        batch_id: int,
        *,
        now_text: str,
    ) -> MaterializationClaim | None:
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if (
                batch is None
                or batch.status != "open"
                or batch.materialization_state != "unmaterialized"
            ):
                return None
            spec = self._state._batch_specs.get(batch.batch_id)
            submission = self._state._compile_submissions_by_key.get(batch.compile_key)
            if spec is None or submission is None:
                return None
            generation = (
                self._state._materialization_generation_by_batch.get(batch.batch_id, 0)
                + 1
            )
            self._state._materialization_generation_by_batch[batch.batch_id] = (
                generation
            )
            batch.materialization_state = "materializing"
            batch.updated_at = now_text
            self._state._touch_batch_locked(batch)
            return MaterializationClaim(
                batch_id=batch.batch_id,
                generation=generation,
                batch=batch_snapshot(batch),
                spec=spec,
                submission=submission,
            )

    def finish_materialization(
        self,
        claim: MaterializationClaim,
        *,
        success: bool,
        materialized_submission: CompileSubmission | None,
        error_text: str,
        now_text: str,
    ) -> bool:
        with self._state._lock:
            batch = self._state._batches.get(claim.batch_id)
            if (
                batch is None
                or batch.status != "open"
                or batch.materialization_state != "materializing"
                or self._state._materialization_generation_by_batch.get(claim.batch_id)
                != claim.generation
            ):
                return False
            if success:
                if materialized_submission is None:
                    raise RuntimeError("materialized compile submission is required")
                original = self._state._compile_submissions_by_key.get(
                    batch.compile_key
                )
                if original is None:
                    raise RuntimeError(
                        "compile submission disappeared during materialization"
                    )
                if self._state._compile_submission_identity(
                    materialized_submission
                ) != self._state._compile_submission_identity(
                    original
                ) or not self._state._compile_submission_is_materialized(
                    materialized_submission
                ):
                    raise RuntimeError(
                        "materialized compile submission identity changed"
                    )
                self._state._compile_submissions_by_key[batch.compile_key] = (
                    materialized_submission
                )
            batch.materialization_state = "ready" if success else "failed"
            if not success:
                batch.failure_runresult = "internal-error"
                batch.failure_text = error_text
            batch.updated_at = now_text
            self._state._touch_batch_locked(batch)
            counts = self._state._batch_counts[batch.batch_id]
            if (
                (
                    batch.verification_id in self._state._closed_verification_ids
                    or (
                        batch.verification_id,
                        batch.verification_program_id,
                    )
                    in self._state._closed_program_keys
                )
                and counts.total > 0
                and counts.terminal == counts.total
            ):
                self._state._close_batch_locked(batch, updated_at=now_text)
            return True

    def claim_lease(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> LeaseClaim | None:
        cap = max(1, min(256, int(limit)))
        with self._state._lock:
            batch = self._state._batches.get(int(batch_id))
            if (
                batch is None
                or batch.status != "open"
                or batch.materialization_state != "ready"
                or batch.compile_state == "failed"
            ):
                return None
            first = self._state._peek_case_heap_locked(batch.batch_id, status="pending")
            if first is None:
                return None
            if (
                self._state._scheduling_policy.single_compile_owner
                and batch.compile_state == "unknown"
            ):
                compile_owner = self._state._compile_owner_by_batch.get(batch.batch_id)
                if compile_owner not in {None, hostname}:
                    return None
                self._state._compile_owner_by_batch[batch.batch_id] = hostname
                cap = 1
            leased: list[JudgehostCaseRow] = []
            while len(leased) < cap:
                case = self._state._peek_case_heap_locked(
                    batch.batch_id, status="pending"
                )
                if case is None:
                    break
                heapq.heappop(self._state._runnable_heaps_by_batch[batch.batch_id])
                case.claim_generation += 1
                self._state._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_batch=False,
                )
                leased.append(case_snapshot(case))
            self._state._refresh_batches_locked({batch.batch_id})
            if not leased:
                return None
            return LeaseClaim(
                batch_id=batch.batch_id,
                hostname=hostname,
                cases=tuple(leased),
                generations=tuple(
                    (row["id"], self._state._cases[row["id"]].claim_generation)
                    for row in leased
                ),
            )

    def commit_lease(self, claim: LeaseClaim) -> bool:
        with self._state._lock:
            return all(
                (case := self._state._cases.get(case_id)) is not None
                and case.status == "leased"
                and case.lease_owner == claim.hostname
                and case.claim_generation == generation
                for case_id, generation in claim.generations
            )

    def abort_lease(self, claim: LeaseClaim, *, now_text: str) -> bool:
        with self._state._lock:
            affected: set[int] = set()
            for case_id, generation in claim.generations:
                case = self._state._cases.get(case_id)
                if (
                    case is None
                    or case.status != "leased"
                    or case.lease_owner != claim.hostname
                    or case.claim_generation != generation
                ):
                    continue
                status = "cancelled" if case.cancel_requested else "pending"
                case.cancel_requested = False
                self._state._transition_case_locked(
                    case,
                    status,
                    lease_owner=None,
                    updated_at=now_text,
                    refresh_batch=False,
                )
                affected.add(case.batch_id)
            self._state._refresh_batches_locked(affected, updated_at=now_text)
            return bool(affected)
