from __future__ import annotations

import heapq
import itertools
import statistics
from collections import defaultdict, deque
from collections.abc import Iterable

from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import (
    CaseRecord,
    ExecutionBatchRecord,
    ExecutionBatchRow,
    HostLeaseRelease,
    JudgehostCaseRow,
)


SELECTION_STAGES = (
    "affinity",
    "stolen",
    "prerequisite",
    "undispatched",
)

STRATEGY_NAMES = (
    "production",
    "legacy-production",
    "naive-no-affinity",
    "affinity-foreground-last",
    "affinity-foreground-first",
    "dispatch-scope-pending-parallel",
)

SCORE_STRATEGY_NAMES = (
    "pending-count",
    "pending-per-active-host",
    "observed-work",
    "observed-work-per-active-host",
    "observed-marginal-saving",
    "oracle-marginal-saving",
)


def selection_strategy_name(
    stages: Iterable[str],
    *,
    parallel_compile: bool = False,
) -> str:
    stage_tuple = tuple(stages)
    if len(set(stage_tuple)) != len(stage_tuple):
        raise ValueError("selection stages must be unique")
    unknown = set(stage_tuple).difference(SELECTION_STAGES)
    if unknown:
        raise ValueError(f"unknown selection stages: {', '.join(sorted(unknown))}")
    prefix = "selection-parallel:" if parallel_compile else "selection:"
    return prefix + ",".join(stage_tuple)


def selection_ablation_strategies() -> tuple[str, tuple[str, ...], dict[str, tuple[str, ...]], tuple[tuple[str, ...], ...]]:
    baseline = SELECTION_STAGES
    omissions = {
        f"without-{stage}": tuple(item for item in baseline if item != stage)
        for stage in baseline
    }
    orders = tuple(itertools.permutations(baseline))
    return selection_strategy_name(baseline), baseline, omissions, orders


class _NaiveNoAffinityScheduler(BatchScheduler):
    """Use only the production global heap, without host context reuse."""

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        with self._lock:
            leased = self._leased_batch_locked(hostname)
            if leased is not None:
                return self._batch_row(leased)
            selected = self._take_ready_batch_locked()
            return None if selected is None else self._batch_row(selected)

    def _leased_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        batch_ids = {
            self._cases[case_id].batch_id
            for case_id in self._leased_case_ids_by_host.get(hostname, ())
            if case_id in self._cases
        }
        if len(batch_ids) != 1:
            return None
        batch = self._batches.get(next(iter(batch_ids)))
        if batch is None or self._batch_next_case_locked(batch, hostname=hostname) is None:
            return None
        return batch


class _AffinityForegroundLastScheduler(BatchScheduler):
    """Production host context, but consult foreground only after local work."""

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        with self._lock:
            leased_batch_ids = {
                self._cases[case_id].batch_id
                for case_id in self._leased_case_ids_by_host.get(hostname, ())
                if case_id in self._cases
            }
            if leased_batch_ids:
                if len(leased_batch_ids) != 1:
                    return None
                leased = self._batches.get(next(iter(leased_batch_ids)))
                if leased is None or self._batch_next_case_locked(leased, hostname=hostname) is None:
                    return None
                return self._batch_row(leased)

            affinity_ids = self._prune_host_context_locked(hostname)
            affinity = self._ready_host_batch_locked(hostname, affinity_ids)
            if affinity is not None:
                return self._batch_row(affinity)

            unblocking = self._unblocking_batch_locked(hostname, affinity_ids)
            if unblocking is not None:
                self._remember_host_batch_locked(hostname, unblocking)
                return self._batch_row(unblocking)

            selected = self._take_ready_batch_locked()
            if selected is None:
                return None
            self._remember_host_batch_locked(hostname, selected)
            return self._batch_row(selected)


class _SelectionStageScheduler(BatchScheduler):
    """Parameterize selection stages while retaining real Scheduler transitions."""

    def __init__(
        self,
        *,
        id_base: int,
        stages: tuple[str, ...],
        parallel_compile: bool,
    ):
        super().__init__(id_base=id_base)
        self._selection_stages = stages
        self._parallel_compile = parallel_compile
        self._sim_dispatched_batch_ids: set[int] = set()
        self._sim_stolen_batch_by_host: dict[str, int] = {}

    def select_ready_batch(self, hostname: str) -> ExecutionBatchRow | None:
        with self._lock:
            leased = self._leased_batch_locked(hostname)
            if leased is not None:
                return self._batch_row(leased)

            foreground = self._foreground_batch_locked(hostname)
            if foreground is not None:
                return self._batch_row(foreground)

            affinity_ids = self._prune_host_context_locked(hostname)
            for stage in self._selection_stages:
                selected = self._select_stage_locked(stage, hostname, affinity_ids)
                if selected is not None:
                    return self._batch_row(selected)

            selected = self._largest_pending_batch_locked(hostname)
            if selected is None:
                return None
            self._mark_dispatched_locked(selected)
            self._remember_or_steal_locked(hostname, selected)
            return self._batch_row(selected)

    def _remember_or_steal_locked(
        self,
        hostname: str,
        batch: ExecutionBatchRecord,
    ) -> None:
        self._remember_host_batch_locked(hostname, batch)
        affinity_ids = self._prune_host_context_locked(hostname)
        if batch.batch_id not in affinity_ids:
            self._sim_stolen_batch_by_host[hostname] = batch.batch_id

    def _leased_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        batch_ids = {
            self._cases[case_id].batch_id
            for case_id in self._leased_case_ids_by_host.get(hostname, ())
            if case_id in self._cases
        }
        if len(batch_ids) != 1:
            return None
        batch = self._batches.get(next(iter(batch_ids)))
        if batch is None or self._batch_next_case_locked(batch, hostname=hostname) is None:
            return None
        return batch

    def _foreground_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        batch = self._peek_ready_batch_locked()
        if batch is None or batch.service_class != "foreground":
            return None
        selected = self._take_ready_batch_locked()
        if selected is not None:
            self._sim_dispatched_batch_ids.add(selected.batch_id)
            self._remember_host_batch_locked(hostname, selected)
        return selected

    def _select_stage_locked(
        self,
        stage: str,
        hostname: str,
        affinity_ids: Iterable[int],
    ) -> ExecutionBatchRecord | None:
        if stage == "affinity":
            return self._ready_host_batch_locked(hostname, affinity_ids)
        if stage == "stolen":
            return self._ready_stolen_batch_locked(hostname)
        if stage == "prerequisite":
            batch = self._unblocking_batch_locked(hostname, affinity_ids)
            if batch is not None:
                self._sim_dispatched_batch_ids.add(batch.batch_id)
                self._remember_or_steal_locked(hostname, batch)
            return batch
        if stage == "undispatched":
            candidates = [
                (case.scope_sequence, batch.batch_id, batch)
                for batch in self._batches.values()
                if batch.batch_id not in self._sim_dispatched_batch_ids
                and (case := self._batch_next_case_locked(batch, hostname=hostname)) is not None
            ]
            if not candidates:
                return None
            selected = min(candidates, key=lambda item: item[:2])[-1]
            self._mark_dispatched_locked(selected)
            self._remember_or_steal_locked(hostname, selected)
            return selected
        raise AssertionError(f"unsupported selection stage: {stage}")

    def _ready_stolen_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        batch_id = self._sim_stolen_batch_by_host.get(hostname)
        batch = None if batch_id is None else self._batches.get(batch_id)
        if batch is not None and self._batch_next_case_locked(batch, hostname=hostname) is not None:
            return batch
        if batch_id is not None:
            self._sim_stolen_batch_by_host.pop(hostname, None)
        return None

    def _largest_pending_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        candidates: list[tuple[int, int, int, ExecutionBatchRecord]] = []
        for batch in self._batches.values():
            case = self._batch_next_case_locked(batch, hostname=hostname)
            if case is None:
                continue
            pending = self._batch_counts[batch.batch_id].pending
            candidates.append((-pending, case.scope_sequence, batch.batch_id, batch))
        return None if not candidates else min(candidates, key=lambda item: item[:3])[-1]

    def _mark_dispatched_locked(self, batch: ExecutionBatchRecord) -> None:
        if batch.batch_id in self._sim_dispatched_batch_ids:
            return
        self._sim_dispatched_batch_ids.add(batch.batch_id)
        self._dispatch_batch_locked(batch)

    def _batch_next_case_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        hostname: str = "",
    ) -> CaseRecord | None:
        if not self._parallel_compile:
            return super()._batch_next_case_locked(batch, hostname=hostname)
        if batch.status != "open":
            return None
        cached = self._peek_case_heap_locked(batch.batch_id, status="cache-pending")
        if cached is not None:
            return cached
        pending = self._peek_case_heap_locked(batch.batch_id, status="pending")
        if pending is None or batch.compile_state == "failed":
            return None
        if batch.materialization_state == "unmaterialized":
            return pending
        return pending if batch.materialization_state == "ready" else None

    def lease_cases(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[JudgehostCaseRow]:
        if not self._parallel_compile:
            return super().lease_cases(
                batch_id,
                hostname=hostname,
                limit=limit,
                now_text=now_text,
            )
        cap = max(1, min(256, int(limit)))
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if (
                batch is None
                or batch.status != "open"
                or batch.materialization_state != "ready"
                or batch.compile_state == "failed"
            ):
                return []
            leased: list[JudgehostCaseRow] = []
            while len(leased) < cap:
                case = self._peek_case_heap_locked(batch.batch_id, status="pending")
                if case is None:
                    break
                heapq.heappop(self._runnable_heaps_by_batch[batch.batch_id])
                self._transition_case_locked(
                    case,
                    "leased",
                    lease_owner=hostname,
                    updated_at=now_text,
                    refresh_batch=False,
                )
                leased.append(self._case_row(case))
            self._refresh_batches_locked({batch.batch_id})
            return leased


class _LegacyProductionScheduler(_SelectionStageScheduler):
    """Reproduce the removed stolen slot and single-owner compile gate."""

    def __init__(self, *, id_base: int):
        super().__init__(
            id_base=id_base,
            stages=SELECTION_STAGES,
            parallel_compile=False,
        )
        self._sim_compile_owner_by_batch: dict[int, str] = {}

    def _batch_next_case_locked(
        self,
        batch: ExecutionBatchRecord,
        *,
        hostname: str = "",
    ) -> CaseRecord | None:
        case = super()._batch_next_case_locked(batch, hostname=hostname)
        if case is None or batch.compile_state != "unknown":
            return case
        owner = self._sim_compile_owner_by_batch.get(batch.batch_id)
        return case if owner in {None, hostname} else None

    def lease_cases(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
        now_text: str,
    ) -> list[JudgehostCaseRow]:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return []
            if batch.compile_state == "unknown":
                owner = self._sim_compile_owner_by_batch.get(batch.batch_id)
                if owner not in {None, hostname}:
                    return []
                self._sim_compile_owner_by_batch[batch.batch_id] = hostname
                limit = 1
        return super().lease_cases(
            batch_id,
            hostname=hostname,
            limit=limit,
            now_text=now_text,
        )

    def release_host_leases(self, hostname: str, *, now_text: str) -> HostLeaseRelease:
        released = super().release_host_leases(hostname, now_text=now_text)
        with self._lock:
            affected = {
                batch_id
                for batch_id, owner in self._sim_compile_owner_by_batch.items()
                if owner == hostname
            }
            for batch_id in affected:
                self._sim_compile_owner_by_batch.pop(batch_id, None)
            self._refresh_batches_locked(affected)
        return released


class _ScoredSelectionScheduler(_SelectionStageScheduler):
    """Compare global fallback scores after foreground, affinity, prerequisite and spread."""

    _DEFAULT_CASE_TIME_SEC = 0.5
    _DEFAULT_COMPILE_TIME_SEC = 2.0

    def __init__(self, *, id_base: int, score_name: str, spread_first: bool):
        super().__init__(
            id_base=id_base,
            stages=(
                ("affinity", "prerequisite", "undispatched")
                if spread_first
                else ("affinity", "prerequisite")
            ),
            parallel_compile=True,
        )
        self._score_name = score_name
        self._case_durations_by_batch: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=10)
        )
        self._recent_case_durations: deque[float] = deque(maxlen=10)
        self._compile_durations_by_key: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=10)
        )
        self._warm_hosts_by_compile_key: dict[str, set[str]] = defaultdict(set)
        self._planned_case_duration_by_id: dict[int, float] = {}
        self._planned_compile_duration_by_batch: dict[int, float] = {}

    def register_simulated_case(
        self,
        *,
        batch_id: int,
        case_id: int,
        case_duration_sec: float,
        compile_duration_sec: float,
    ) -> None:
        with self._lock:
            self._planned_case_duration_by_id[int(case_id)] = float(case_duration_sec)
            self._planned_compile_duration_by_batch[int(batch_id)] = float(
                compile_duration_sec
            )

    def observe_simulated_compile(
        self,
        *,
        batch_id: int,
        hostname: str,
        duration_sec: float,
    ) -> None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return
            self._compile_durations_by_key[batch.compile_key].append(float(duration_sec))
            self._warm_hosts_by_compile_key[batch.compile_key].add(hostname)

    def observe_simulated_case(
        self,
        *,
        batch_id: int,
        hostname: str,
        duration_sec: float,
    ) -> None:
        with self._lock:
            batch = self._batches.get(int(batch_id))
            if batch is None:
                return
            duration = float(duration_sec)
            self._case_durations_by_batch[batch.batch_id].append(duration)
            self._recent_case_durations.append(duration)
            self._warm_hosts_by_compile_key[batch.compile_key].add(hostname)

    def _largest_pending_batch_locked(self, hostname: str) -> ExecutionBatchRecord | None:
        candidates: list[tuple[float, int, int, ExecutionBatchRecord]] = []
        for batch in self._batches.values():
            case = self._batch_next_case_locked(batch, hostname=hostname)
            if case is None:
                continue
            score = self._fallback_score_locked(batch, hostname)
            candidates.append((-score, case.scope_sequence, batch.batch_id, batch))
        return None if not candidates else min(candidates, key=lambda item: item[:3])[-1]

    def _fallback_score_locked(
        self,
        batch: ExecutionBatchRecord,
        hostname: str,
    ) -> float:
        pending_count = self._batch_counts[batch.batch_id].pending
        active_hosts = self._active_hosts_locked(batch.batch_id)
        host_divisor = len(active_hosts) + (hostname not in active_hosts)
        host_divisor = max(1, host_divisor)
        if self._score_name == "pending-count":
            return float(pending_count)
        if self._score_name == "pending-per-active-host":
            return pending_count / host_divisor

        if self._score_name == "oracle-marginal-saving":
            work = sum(
                self._planned_case_duration_by_id.get(case_id, 0.0)
                for case_id in self._case_ids_by_batch[batch.batch_id]
                if self._cases[case_id].status == "pending"
            )
            compile_cost = self._planned_compile_duration_by_batch.get(
                batch.batch_id,
                self._DEFAULT_COMPILE_TIME_SEC,
            )
            return self._marginal_saving_locked(
                batch,
                hostname,
                work=work,
                compile_cost=compile_cost,
                active_host_count=len(active_hosts),
            )

        case_time = self._observed_case_time_locked(batch.batch_id)
        work = pending_count * case_time
        if self._score_name == "observed-work":
            return work
        if self._score_name == "observed-work-per-active-host":
            return work / host_divisor
        if self._score_name == "observed-marginal-saving":
            return self._marginal_saving_locked(
                batch,
                hostname,
                work=work,
                compile_cost=self._observed_compile_time_locked(batch.compile_key),
                active_host_count=len(active_hosts),
            )
        raise AssertionError(f"unsupported score strategy: {self._score_name}")

    def _active_hosts_locked(self, batch_id: int) -> set[str]:
        return {
            case.lease_owner
            for case_id in self._case_ids_by_batch[batch_id]
            if (case := self._cases[case_id]).status in {"leased", "reporting"}
            and case.lease_owner is not None
        }

    def _observed_case_time_locked(self, batch_id: int) -> float:
        samples = self._case_durations_by_batch.get(batch_id)
        if samples:
            return float(statistics.median(samples))
        if self._recent_case_durations:
            return float(statistics.median(self._recent_case_durations))
        return self._DEFAULT_CASE_TIME_SEC

    def _observed_compile_time_locked(self, compile_key: str) -> float:
        samples = self._compile_durations_by_key.get(compile_key)
        return (
            self._DEFAULT_COMPILE_TIME_SEC
            if not samples
            else float(statistics.median(samples))
        )

    def _marginal_saving_locked(
        self,
        batch: ExecutionBatchRecord,
        hostname: str,
        *,
        work: float,
        compile_cost: float,
        active_host_count: int,
    ) -> float:
        workers = max(1, active_host_count)
        parallel_saving = work / (workers * (workers + 1))
        if hostname in self._warm_hosts_by_compile_key.get(batch.compile_key, ()):
            compile_cost = 0.0
        return parallel_saving - compile_cost


def register_simulated_case(
    scheduler: BatchScheduler,
    *,
    batch_id: int,
    case_id: int,
    case_duration_sec: float,
    compile_duration_sec: float,
) -> None:
    if isinstance(scheduler, _ScoredSelectionScheduler):
        scheduler.register_simulated_case(
            batch_id=batch_id,
            case_id=case_id,
            case_duration_sec=case_duration_sec,
            compile_duration_sec=compile_duration_sec,
        )


def observe_simulated_compile(
    scheduler: BatchScheduler,
    *,
    batch_id: int,
    hostname: str,
    duration_sec: float,
) -> None:
    if isinstance(scheduler, _ScoredSelectionScheduler):
        scheduler.observe_simulated_compile(
            batch_id=batch_id,
            hostname=hostname,
            duration_sec=duration_sec,
        )


def observe_simulated_case(
    scheduler: BatchScheduler,
    *,
    batch_id: int,
    hostname: str,
    duration_sec: float,
) -> None:
    if isinstance(scheduler, _ScoredSelectionScheduler):
        scheduler.observe_simulated_case(
            batch_id=batch_id,
            hostname=hostname,
            duration_sec=duration_sec,
        )


def _parse_selection_strategy(strategy: str) -> tuple[tuple[str, ...], bool] | None:
    prefixes = (("selection-parallel:", True), ("selection:", False))
    match = next(
        ((prefix, parallel) for prefix, parallel in prefixes if strategy.startswith(prefix)),
        None,
    )
    if match is None:
        return None
    prefix, parallel_compile = match
    payload = strategy.removeprefix(prefix)
    stages = () if not payload else tuple(payload.split(","))
    selection_strategy_name(stages)
    return stages, parallel_compile


def create_scheduler(strategy: str, *, id_base: int) -> BatchScheduler:
    if strategy == "dispatch-scope-pending-parallel":
        return BatchScheduler(id_base=id_base)
    score_prefixes = (
        ("score-parallel:", True),
        ("score-global-parallel:", False),
    )
    score_match = next(
        (
            (prefix, spread_first)
            for prefix, spread_first in score_prefixes
            if strategy.startswith(prefix)
        ),
        None,
    )
    if score_match is not None:
        prefix, spread_first = score_match
        score_name = strategy.removeprefix(prefix)
        if score_name not in SCORE_STRATEGY_NAMES:
            raise ValueError(f"unknown score strategy: {score_name}")
        return _ScoredSelectionScheduler(
            id_base=id_base,
            score_name=score_name,
            spread_first=spread_first,
        )
    selection_policy = _parse_selection_strategy(strategy)
    if selection_policy is not None:
        selection_stages, parallel_compile = selection_policy
        return _SelectionStageScheduler(
            id_base=id_base,
            stages=selection_stages,
            parallel_compile=parallel_compile,
        )
    if strategy == "naive-no-affinity":
        return _NaiveNoAffinityScheduler(id_base=id_base)
    if strategy == "legacy-production":
        return _LegacyProductionScheduler(id_base=id_base)
    if strategy == "affinity-foreground-last":
        return _AffinityForegroundLastScheduler(id_base=id_base)
    if strategy not in STRATEGY_NAMES:
        raise ValueError(f"unknown simulation strategy: {strategy}")
    # The explicit foreground-first variant is intentionally identical to production.
    # Keeping both labels makes comparison reports state that fact instead of assuming it.
    return BatchScheduler(id_base=id_base)
