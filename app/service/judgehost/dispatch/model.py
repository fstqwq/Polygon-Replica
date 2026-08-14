from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchOutcome:
    work: tuple[dict[str, object], ...]
    terminal_batch_ids: tuple[int, ...]


@dataclass(frozen=True)
class CacheProbeOutcome:
    pending_task_ids: frozenset[str]
    terminal_batch_ids: tuple[int, ...]


@dataclass(frozen=True)
class HostRegistrationOutcome:
    workdirs: tuple[dict[str, object], ...]
    terminal_batch_ids: tuple[int, ...]
