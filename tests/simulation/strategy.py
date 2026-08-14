import itertools
import statistics
from collections import defaultdict, deque
from collections.abc import Iterable

from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.policy import (
    SchedulingCandidate,
    SchedulingDecision,
    SchedulingPolicy,
    SchedulingSnapshot,
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


def selection_ablation_strategies() -> (
    tuple[str, tuple[str, ...], dict[str, tuple[str, ...]], tuple[tuple[str, ...], ...]]
):
    baseline = SELECTION_STAGES
    omissions = {
        f"without-{stage}": tuple(item for item in baseline if item != stage) for stage in baseline
    }
    orders = tuple(itertools.permutations(baseline))
    return selection_strategy_name(baseline), baseline, omissions, orders


class _NaiveNoAffinityPolicy:
    single_compile_owner = False
    catalog_all_candidates = True

    def select(self, snapshot: SchedulingSnapshot) -> SchedulingDecision | None:
        if snapshot.leased_batch_id is not None:
            return _decision(snapshot.leased_batch_id)
        return _decision(snapshot.global_batch_id, increment=True)


class _AffinityForegroundLastPolicy:
    single_compile_owner = False
    catalog_all_candidates = True

    def select(self, snapshot: SchedulingSnapshot) -> SchedulingDecision | None:
        if snapshot.leased_batch_id is not None:
            return _decision(snapshot.leased_batch_id)
        if snapshot.affinity_ready_batch_id is not None:
            return _decision(snapshot.affinity_ready_batch_id)
        if snapshot.prerequisite_batch_id is not None:
            return _decision(
                snapshot.prerequisite_batch_id,
                increment=True,
                remember=True,
            )
        return _decision(snapshot.global_batch_id, increment=True, remember=True)


class _SelectionStagePolicy:
    single_compile_owner = False
    catalog_all_candidates = True

    def __init__(self, *, stages: tuple[str, ...]) -> None:
        self._stages = stages

    def select(self, snapshot: SchedulingSnapshot) -> SchedulingDecision | None:
        if snapshot.leased_batch_id is not None:
            return _decision(snapshot.leased_batch_id)
        if snapshot.foreground_batch_id is not None:
            return _decision(
                snapshot.foreground_batch_id,
                increment=True,
                remember=True,
            )
        for stage in self._stages:
            if stage == "affinity" and snapshot.affinity_ready_batch_id is not None:
                return _decision(snapshot.affinity_ready_batch_id)
            if stage == "stolen" and snapshot.stolen_batch_id is not None:
                return _decision(snapshot.stolen_batch_id)
            if stage == "prerequisite" and snapshot.prerequisite_batch_id is not None:
                return _decision(
                    snapshot.prerequisite_batch_id,
                    increment=True,
                    remember=True,
                    stolen=True,
                )
            if stage == "undispatched":
                undispatched = [
                    candidate for candidate in snapshot.candidates if candidate.dispatch_count == 0
                ]
                if undispatched:
                    selected = min(
                        undispatched,
                        key=lambda candidate: (
                            candidate.scope_sequence,
                            candidate.batch_id,
                        ),
                    )
                    return _decision(
                        selected.batch_id,
                        increment=True,
                        remember=True,
                        stolen=True,
                    )
            if stage not in SELECTION_STAGES:
                raise AssertionError(f"unsupported selection stage: {stage}")
        selected = self._fallback_candidate(snapshot)
        if selected is None:
            return None
        return _decision(
            selected.batch_id,
            increment=selected.dispatch_count == 0,
            remember=True,
            stolen=True,
        )

    def _fallback_candidate(
        self,
        snapshot: SchedulingSnapshot,
    ) -> SchedulingCandidate | None:
        if not snapshot.candidates:
            return None
        return min(
            snapshot.candidates,
            key=lambda candidate: (
                -candidate.pending_count,
                candidate.scope_sequence,
                candidate.batch_id,
            ),
        )


class _LegacyProductionPolicy(_SelectionStagePolicy):
    single_compile_owner = True

    def __init__(self) -> None:
        super().__init__(stages=SELECTION_STAGES)


class _ScoredSelectionPolicy(_SelectionStagePolicy):
    _DEFAULT_CASE_TIME_SEC = 0.5
    _DEFAULT_COMPILE_TIME_SEC = 2.0

    def __init__(self, *, score_name: str, spread_first: bool) -> None:
        super().__init__(
            stages=(
                ("affinity", "prerequisite", "undispatched")
                if spread_first
                else ("affinity", "prerequisite")
            )
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
        self._compile_key_by_batch: dict[int, str] = {}

    def register_simulated_case(
        self,
        *,
        batch_id: int,
        case_id: int,
        compile_key: str,
        case_duration_sec: float,
        compile_duration_sec: float,
    ) -> None:
        self._compile_key_by_batch[int(batch_id)] = compile_key
        self._planned_case_duration_by_id[int(case_id)] = float(case_duration_sec)
        self._planned_compile_duration_by_batch[int(batch_id)] = float(compile_duration_sec)

    def observe_simulated_compile(
        self,
        *,
        batch_id: int,
        hostname: str,
        duration_sec: float,
    ) -> None:
        compile_key = self._compile_key_by_batch.get(int(batch_id))
        if compile_key is None:
            return
        self._compile_durations_by_key[compile_key].append(float(duration_sec))
        self._warm_hosts_by_compile_key[compile_key].add(hostname)

    def observe_simulated_case(
        self,
        *,
        batch_id: int,
        hostname: str,
        duration_sec: float,
    ) -> None:
        duration = float(duration_sec)
        self._case_durations_by_batch[int(batch_id)].append(duration)
        self._recent_case_durations.append(duration)
        compile_key = self._compile_key_by_batch.get(int(batch_id))
        if compile_key is not None:
            self._warm_hosts_by_compile_key[compile_key].add(hostname)

    def _fallback_candidate(
        self,
        snapshot: SchedulingSnapshot,
    ) -> SchedulingCandidate | None:
        if not snapshot.candidates:
            return None
        return min(
            snapshot.candidates,
            key=lambda candidate: (
                -self._score(candidate, snapshot.hostname),
                candidate.scope_sequence,
                candidate.batch_id,
            ),
        )

    def _score(self, candidate: SchedulingCandidate, hostname: str) -> float:
        pending_count = candidate.pending_count
        host_divisor = max(
            1,
            len(candidate.active_hosts) + (hostname not in candidate.active_hosts),
        )
        if self._score_name == "pending-count":
            return float(pending_count)
        if self._score_name == "pending-per-active-host":
            return pending_count / host_divisor
        if self._score_name == "oracle-marginal-saving":
            work = sum(
                self._planned_case_duration_by_id.get(case_id, 0.0)
                for case_id in candidate.pending_case_ids
            )
            compile_cost = self._planned_compile_duration_by_batch.get(
                candidate.batch_id,
                self._DEFAULT_COMPILE_TIME_SEC,
            )
            return self._marginal_saving(
                candidate,
                hostname,
                work=work,
                compile_cost=compile_cost,
            )
        case_time = self._observed_case_time(candidate.batch_id)
        work = pending_count * case_time
        if self._score_name == "observed-work":
            return work
        if self._score_name == "observed-work-per-active-host":
            return work / host_divisor
        if self._score_name == "observed-marginal-saving":
            return self._marginal_saving(
                candidate,
                hostname,
                work=work,
                compile_cost=self._observed_compile_time(candidate.compile_key),
            )
        raise AssertionError(f"unsupported score strategy: {self._score_name}")

    def _observed_case_time(self, batch_id: int) -> float:
        samples = self._case_durations_by_batch.get(batch_id)
        if samples:
            return float(statistics.median(samples))
        if self._recent_case_durations:
            return float(statistics.median(self._recent_case_durations))
        return self._DEFAULT_CASE_TIME_SEC

    def _observed_compile_time(self, compile_key: str) -> float:
        samples = self._compile_durations_by_key.get(compile_key)
        return self._DEFAULT_COMPILE_TIME_SEC if not samples else float(statistics.median(samples))

    def _marginal_saving(
        self,
        candidate: SchedulingCandidate,
        hostname: str,
        *,
        work: float,
        compile_cost: float,
    ) -> float:
        workers = max(1, len(candidate.active_hosts))
        parallel_saving = work / (workers * (workers + 1))
        if hostname in self._warm_hosts_by_compile_key.get(candidate.compile_key, ()):
            compile_cost = 0.0
        return parallel_saving - compile_cost


def _decision(
    batch_id: int | None,
    *,
    increment: bool = False,
    remember: bool = False,
    stolen: bool = False,
) -> SchedulingDecision | None:
    if batch_id is None:
        return None
    return SchedulingDecision(
        batch_id=batch_id,
        increment_dispatch=increment,
        remember=remember,
        track_as_stolen=stolen,
    )


_SIMULATION_POLICIES: dict[JudgehostBatchRuntime, SchedulingPolicy] = {}


def register_simulated_case(
    scheduler: JudgehostBatchRuntime,
    *,
    batch_id: int,
    case_id: int,
    compile_key: str,
    case_duration_sec: float,
    compile_duration_sec: float,
) -> None:
    policy = _SIMULATION_POLICIES.get(scheduler)
    if isinstance(policy, _ScoredSelectionPolicy):
        policy.register_simulated_case(
            batch_id=batch_id,
            case_id=case_id,
            compile_key=compile_key,
            case_duration_sec=case_duration_sec,
            compile_duration_sec=compile_duration_sec,
        )


def observe_simulated_compile(
    scheduler: JudgehostBatchRuntime,
    *,
    batch_id: int,
    hostname: str,
    duration_sec: float,
) -> None:
    policy = _SIMULATION_POLICIES.get(scheduler)
    if isinstance(policy, _ScoredSelectionPolicy):
        policy.observe_simulated_compile(
            batch_id=batch_id,
            hostname=hostname,
            duration_sec=duration_sec,
        )


def observe_simulated_case(
    scheduler: JudgehostBatchRuntime,
    *,
    batch_id: int,
    hostname: str,
    duration_sec: float,
) -> None:
    policy = _SIMULATION_POLICIES.get(scheduler)
    if isinstance(policy, _ScoredSelectionPolicy):
        policy.observe_simulated_case(
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


def create_scheduler(strategy: str, *, id_base: int) -> JudgehostBatchRuntime:
    if strategy == "dispatch-scope-pending-parallel":
        return JudgehostBatchRuntime(id_base=id_base)
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
        policy = _ScoredSelectionPolicy(
            score_name=score_name,
            spread_first=spread_first,
        )
        runtime = JudgehostBatchRuntime(
            id_base=id_base,
            scheduling_policy=policy,
        )
        _SIMULATION_POLICIES[runtime] = policy
        return runtime
    selection_policy = _parse_selection_strategy(strategy)
    if selection_policy is not None:
        selection_stages, _parallel_compile = selection_policy
        policy = _SelectionStagePolicy(
            stages=selection_stages,
        )
        runtime = JudgehostBatchRuntime(
            id_base=id_base,
            scheduling_policy=policy,
        )
        _SIMULATION_POLICIES[runtime] = policy
        return runtime
    if strategy == "naive-no-affinity":
        return JudgehostBatchRuntime(
            id_base=id_base,
            scheduling_policy=_NaiveNoAffinityPolicy(),
        )
    if strategy == "legacy-production":
        return JudgehostBatchRuntime(
            id_base=id_base,
            scheduling_policy=_LegacyProductionPolicy(),
        )
    if strategy == "affinity-foreground-last":
        return JudgehostBatchRuntime(
            id_base=id_base,
            scheduling_policy=_AffinityForegroundLastPolicy(),
        )
    if strategy not in STRATEGY_NAMES:
        raise ValueError(f"unknown simulation strategy: {strategy}")
    # The explicit foreground-first variant is intentionally identical to production.
    # Keeping both labels makes comparison reports state that fact instead of assuming it.
    return JudgehostBatchRuntime(id_base=id_base)
