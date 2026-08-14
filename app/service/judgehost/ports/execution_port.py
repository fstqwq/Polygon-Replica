from typing import Protocol

from app.service.judgehost.ports.case_binding import CaseBindingPort
from app.service.judgehost.ports.completion import (
    CaseCompletionSink,
    CaseDiagnosticSink,
    CaseLeaseSink,
)


class JudgehostExecutionPort(
    CaseBindingPort,
    CaseCompletionSink,
    CaseDiagnosticSink,
    CaseLeaseSink,
    Protocol,
):
    """All durable execution interaction available to Judgehost."""
