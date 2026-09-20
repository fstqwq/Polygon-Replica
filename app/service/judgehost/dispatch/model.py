from dataclasses import dataclass

from app.service.judgehost.domjudge.wire_model import DomjudgeWork, DomjudgeWorkdir


@dataclass(frozen=True)
class DispatchOutcome:
    work: tuple[DomjudgeWork, ...]
    terminal_batch_ids: tuple[int, ...]


@dataclass(frozen=True)
class CacheProbeOutcome:
    pending_task_ids: frozenset[str]
    terminal_batch_ids: tuple[int, ...]


@dataclass(frozen=True)
class HostRegistrationOutcome:
    workdirs: tuple[DomjudgeWorkdir, ...]
    terminal_batch_ids: tuple[int, ...]
