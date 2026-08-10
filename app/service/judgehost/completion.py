from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.service.judgehost.case_result import CaseTerminalReport


@dataclass(frozen=True)
class CaseCompletionReport:
    verification_task_id: str
    judgehost_task_id: str
    test_name: str
    report: CaseTerminalReport
    verification_id: str = ""


class CaseCompletionSink(Protocol):
    def reported_many(
        self,
        reports: tuple[CaseCompletionReport, ...],
    ) -> bool: ...

    def cancelled(
        self,
        verification_task_id: str,
        judgehost_task_id: str,
        test_name: str,
        reason: str,
    ) -> bool: ...


class CaseLeaseSink(Protocol):
    def case_leased(
        self,
        verification_id: str,
        verification_task_id: str,
    ) -> bool: ...


DiagnosticAppendOutcome = Literal["persisted", "duplicate", "not-applicable"]


@dataclass(frozen=True)
class DiagnosticAppendResult:
    outcome: DiagnosticAppendOutcome


class CaseDiagnosticSink(Protocol):
    def append(
        self,
        *,
        task_id: str,
        kind: str,
        hostname: str,
        text: str,
        received_at: str,
    ) -> DiagnosticAppendResult: ...
