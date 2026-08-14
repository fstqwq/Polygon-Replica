from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SchedulingCandidate:
    """Immutable scheduling facts for one currently dispatchable batch."""

    batch_id: int
    service_class: str
    scope_sequence: int
    pending_count: int
    dispatch_count: int
    compile_key: str
    compile_state: str
    materialization_state: str
    active_hosts: frozenset[str]
    pending_case_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SchedulingSnapshot:
    """One lock-consistent input to a scheduling policy."""

    hostname: str
    leased_batch_id: int | None
    foreground_batch_id: int | None
    affinity_batch_ids: tuple[int, ...]
    affinity_ready_batch_id: int | None
    stolen_batch_id: int | None
    prerequisite_batch_id: int | None
    global_batch_id: int | None
    candidates: tuple[SchedulingCandidate, ...]


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    """A requested selection; BatchState validates and applies its mutations."""

    batch_id: int
    increment_dispatch: bool
    remember: bool
    track_as_stolen: bool = False


class SchedulingPolicy(Protocol):
    """Pure candidate selection over an immutable scheduling snapshot."""

    single_compile_owner: bool
    catalog_all_candidates: bool

    def select(self, snapshot: SchedulingSnapshot) -> SchedulingDecision | None:
        ...


class ProductionSchedulingPolicy:
    """The production foreground, affinity, prerequisite, global ordering."""

    single_compile_owner = False
    catalog_all_candidates = False

    def select(self, snapshot: SchedulingSnapshot) -> SchedulingDecision | None:
        if snapshot.leased_batch_id is not None:
            return SchedulingDecision(
                batch_id=snapshot.leased_batch_id,
                increment_dispatch=False,
                remember=False,
            )
        if snapshot.foreground_batch_id is not None:
            return SchedulingDecision(
                batch_id=snapshot.foreground_batch_id,
                increment_dispatch=True,
                remember=True,
            )
        if snapshot.affinity_ready_batch_id is not None:
            return SchedulingDecision(
                batch_id=snapshot.affinity_ready_batch_id,
                increment_dispatch=False,
                remember=False,
            )
        if snapshot.prerequisite_batch_id is not None:
            return SchedulingDecision(
                batch_id=snapshot.prerequisite_batch_id,
                increment_dispatch=True,
                remember=True,
            )
        if snapshot.global_batch_id is None:
            return None
        return SchedulingDecision(
            batch_id=snapshot.global_batch_id,
            increment_dispatch=True,
            remember=True,
        )
