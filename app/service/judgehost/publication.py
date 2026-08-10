from __future__ import annotations

from app.db import now_iso
from app.service.judgehost.batch_scheduler_models import ProgramTerminalClaim
from app.service.judgehost.completion import CaseCompletionReport
from app.service.judgehost.shared import domjudge_lower_text, domjudge_text
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.task_queue import TaskQueue
from app.service.platform.error_text import aux_display_text_limit_bytes


class JudgehostCaseDiagnosticPublisher:
    """Stage primary diagnostics and persist diagnostics after a decision."""

    def __init__(self, state: JudgehostState) -> None:
        self._s = state

    def claim_internal_error(
        self,
        case_id: int,
        *,
        hostname: str,
        failure_text: str,
        diagnostic_text: str,
        receipt_generation: int,
    ) -> ProgramTerminalClaim:
        return self._s.batch_scheduler.claim_internal_error(
            int(case_id),
            hostname=hostname,
            failure_text=failure_text,
            diagnostic_text=diagnostic_text,
            receipt_generation=receipt_generation,
            diagnostic_limit_bytes=aux_display_text_limit_bytes(
                self._s.config_values.snapshot()
            ),
            updated_at=now_iso(),
        )

    def record_debug_info(
        self,
        case_id: int,
        *,
        hostname: str,
        text: str,
        receipt_generation: int,
    ) -> str | None:
        return self._s.batch_scheduler.record_case_diagnostic(
            int(case_id),
            kind="debug-info",
            hostname=hostname,
            text=text,
            receipt_generation=receipt_generation,
            diagnostic_limit_bytes=aux_display_text_limit_bytes(
                self._s.config_values.snapshot()
            ),
            now_text=now_iso(),
        )

    def flush_pending(self, case_id: int) -> None:
        case = self._s.batch_scheduler.fetch_case(int(case_id))
        if case is None or not bool(case["completion_acknowledged"]):
            return
        verification_task_id = domjudge_text(case["verification_task_id"])
        if not verification_task_id:
            return
        for diagnostic in self._s.batch_scheduler.pending_case_diagnostics(
            int(case_id)
        ):
            outcome = self._s.case_diagnostic_sink.append(
                task_id=verification_task_id,
                kind=diagnostic.kind,
                hostname=diagnostic.hostname,
                text=diagnostic.text,
                received_at=diagnostic.received_at,
            )
            if outcome.outcome not in {"persisted", "duplicate"}:
                raise RuntimeError(
                    "verification task rejected a pending judgehost diagnostic"
                )
            self._s.batch_scheduler.acknowledge_case_diagnostic(
                int(case_id),
                diagnostic,
            )


class JudgehostCaseCompletionPublisher:
    """Publish terminal Case decisions before acknowledging wire callbacks."""

    def __init__(
        self,
        state: JudgehostState,
        queue: TaskQueue,
        diagnostic_publisher: JudgehostCaseDiagnosticPublisher,
    ) -> None:
        self._s = state
        self._queue = queue
        self._diagnostic_publisher = diagnostic_publisher

    def _reported_case_completion(
        self,
        case_id: int,
    ) -> CaseCompletionReport | None:
        case = self._s.batch_scheduler.fetch_case(int(case_id))
        if case is None:
            return None
        batch = self._s.batch_scheduler.fetch_batch(int(case["batch_id"]))
        if batch is None:
            return None
        judgehost_task_id = domjudge_text(case["task_id"])
        test_name = domjudge_text(case["test_name"])
        if not judgehost_task_id or not test_name:
            return None
        report = self._queue.poll_task_case_result(
            judgehost_task_id,
            test_name,
        )
        if report is None:
            return None
        return CaseCompletionReport(
            verification_task_id=domjudge_text(
                case["verification_task_id"]
            ),
            judgehost_task_id=judgehost_task_id,
            test_name=test_name,
            report=report,
            verification_id=domjudge_text(batch["verification_id"]),
        )

    def publish_reported_cases(
        self,
        case_ids: tuple[int, ...],
    ) -> bool:
        unique_case_ids = tuple(
            dict.fromkeys(int(case_id) for case_id in case_ids)
        )
        reports: list[CaseCompletionReport] = []
        for case_id in unique_case_ids:
            report = self._reported_case_completion(case_id)
            if report is None:
                return False
            reports.append(report)
        if not reports:
            return True
        if not self._s.case_completion_sink.reported_many(tuple(reports)):
            return False
        self._s.batch_scheduler.acknowledge_case_completions(
            list(unique_case_ids)
        )
        return all(
            (case := self._s.batch_scheduler.fetch_case(case_id)) is not None
            and bool(case["completion_acknowledged"])
            for case_id in unique_case_ids
        )

    def _publish_cancelled(
        self,
        *,
        verification_task_id: str,
        task_id: str,
        test_name: str,
        reason: str,
    ) -> bool:
        return self._s.case_completion_sink.cancelled(
            verification_task_id,
            task_id,
            test_name,
            reason,
        )

    def acknowledge_terminal_case(
        self,
        case_id: int,
        *,
        reason: str = "",
    ) -> bool:
        case = self._s.batch_scheduler.fetch_case(int(case_id))
        if case is None:
            return True
        status = domjudge_lower_text(case["status"])
        if status not in {"reported", "cancelled"}:
            return False
        if not bool(case["completion_acknowledged"]):
            if status == "reported":
                accepted = self.publish_reported_cases((int(case_id),))
            else:
                accepted = self._publish_cancelled(
                    verification_task_id=domjudge_text(
                        case["verification_task_id"]
                    ),
                    task_id=domjudge_text(case["task_id"]),
                    test_name=domjudge_text(case["test_name"]),
                    reason=reason,
                )
                if accepted:
                    self._s.batch_scheduler.acknowledge_case_completion(
                        int(case_id)
                    )
            if not accepted:
                raise RuntimeError(
                    "verification task completion was rejected"
                )
        self._diagnostic_publisher.flush_pending(int(case_id))
        return True
