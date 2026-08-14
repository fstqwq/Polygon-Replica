from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from app.service.execution.model import ExecutionResult
from app.service.judgehost.ports.case_binding import CaseBinding


class CaseTerminalReport(TypedDict):
    """Canonical durable report emitted for one terminal execution case."""

    task_id: str
    verification_id: str
    run_id: str
    artifact_path: str
    status: str
    task_status: str
    error: str
    summary: dict[str, object]
    missing_case_result: bool
    execution_result: ExecutionResult


@dataclass(frozen=True)
class CaseCompletionReport:
    binding: CaseBinding
    judgehost_task_id: str
    report: CaseTerminalReport


class CaseCompletionSink(Protocol):
    def reported_many(
        self,
        reports: tuple[CaseCompletionReport, ...],
    ) -> bool:
        ...

    def cancelled(
        self,
        binding: CaseBinding,
        judgehost_task_id: str,
        reason: str,
    ) -> bool:
        ...


class CaseLeaseSink(Protocol):
    def case_leased(self, binding: CaseBinding) -> bool:
        ...


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
    ) -> DiagnosticAppendResult:
        ...
