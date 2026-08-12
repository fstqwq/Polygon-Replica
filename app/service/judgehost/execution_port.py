from typing import Protocol

from app.service.judgehost.case_binding import CaseBindingPort
from app.service.judgehost.completion import (
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
