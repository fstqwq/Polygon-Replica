from dataclasses import dataclass
from typing import Literal, Protocol

from app.service.judgehost.case_binding import CaseBinding
from app.service.judgehost.case_result import CaseTerminalReport


@dataclass(frozen=True)
class CaseCompletionReport:
    binding: CaseBinding
    judgehost_task_id: str
    report: CaseTerminalReport


class CaseCompletionSink(Protocol):
    def reported_many(
        self,
        reports: tuple[CaseCompletionReport, ...],
    ) -> bool: ...

    def cancelled(
        self,
        binding: CaseBinding,
        judgehost_task_id: str,
        reason: str,
    ) -> bool: ...


class CaseLeaseSink(Protocol):
    def case_leased(self, binding: CaseBinding) -> bool: ...


DiagnosticAppendOutcome = Literal["persisted", "duplicate", "not-applicable"]


@dataclass(frozen=True)
class DiagnosticAppendResult:
    outcome: DiagnosticAppendOutcome


class CaseDiagnosticSink(Protocol):
    def append(
        self,
        *,
        binding: CaseBinding,
        kind: str,
        hostname: str,
        text: str,
        received_at: str,
    ) -> DiagnosticAppendResult: ...
