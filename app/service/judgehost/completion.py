from __future__ import annotations

from typing import Protocol

from app.service.judgehost.case_result import CaseTerminalReport


class CaseCompletionSink(Protocol):
    def reported(
        self,
        judgehost_task_id: str,
        test_name: str,
        report: CaseTerminalReport,
        verification_id: str = "",
    ) -> bool: ...

    def cancelled(
        self,
        judgehost_task_id: str,
        test_name: str,
        reason: str,
    ) -> bool: ...

    def amend_debug(
        self,
        judgehost_task_id: str,
        test_name: str,
        report: CaseTerminalReport,
    ) -> bool: ...
