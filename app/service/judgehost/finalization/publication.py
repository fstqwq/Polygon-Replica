from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.ports.case_binding import CaseBinding
from app.service.judgehost.ports.completion import (
    CaseCompletionReport,
    CaseCompletionSink,
    CaseDiagnosticSink,
)
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.result_view import project_task_case_result


class JudgehostCaseDiagnosticPublisher:
    """Stage primary diagnostics and persist diagnostics after a decision."""

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        execution_port: CaseDiagnosticSink,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._execution_port = execution_port

    def flush_pending(self, case_id: int) -> None:
        case = self._batch_runtime.fetch_case(int(case_id))
        if case is None or not bool(case["completion_acknowledged"]):
            return
        verification_task_id = case["verification_task_id"]
        if not verification_task_id:
            return
        batch = self._batch_runtime.fetch_batch(int(case["batch_id"]))
        if batch is None:
            return
        binding = CaseBinding(
            execution_scope_id=batch["verification_id"],
            program_id=batch["verification_program_id"],
            task_id=verification_task_id,
            test_name=case["test_name"],
        )
        for diagnostic in self._batch_runtime.pending_case_diagnostics(int(case_id)):
            outcome = self._execution_port.append(
                binding=binding,
                kind=diagnostic.kind,
                hostname=diagnostic.hostname,
                text=diagnostic.text,
                received_at=diagnostic.received_at,
            )
            if outcome.outcome not in {"persisted", "duplicate"}:
                raise RuntimeError("verification task rejected a pending judgehost diagnostic")
            self._batch_runtime.acknowledge_case_diagnostic(
                int(case_id),
                diagnostic,
            )


class JudgehostCaseCompletionPublisher:
    """Publish terminal Case decisions before acknowledging wire callbacks."""

    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        execution_port: CaseCompletionSink,
        tasks: JudgehostTaskRegistry,
        diagnostic_publisher: JudgehostCaseDiagnosticPublisher,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._execution_port = execution_port
        self._tasks = tasks
        self._diagnostic_publisher = diagnostic_publisher

    def _reported_case_completion(
        self,
        case_id: int,
    ) -> CaseCompletionReport | None:
        case = self._batch_runtime.fetch_case(int(case_id))
        if case is None:
            return None
        batch = self._batch_runtime.fetch_batch(int(case["batch_id"]))
        if batch is None:
            return None
        judgehost_task_id = case["task_id"]
        test_name = case["test_name"]
        if not judgehost_task_id or not test_name:
            return None
        report = project_task_case_result(
            self._tasks,
            self._batch_runtime,
            judgehost_task_id,
            test_name,
        )
        if report is None:
            return None
        return CaseCompletionReport(
            binding=CaseBinding(
                execution_scope_id=batch["verification_id"],
                program_id=batch["verification_program_id"],
                task_id=case["verification_task_id"],
                test_name=test_name,
            ),
            judgehost_task_id=judgehost_task_id,
            report=report,
        )

    def publish_reported_cases(
        self,
        case_ids: tuple[int, ...],
    ) -> bool:
        unique_case_ids = tuple(dict.fromkeys(int(case_id) for case_id in case_ids))
        reports: list[CaseCompletionReport] = []
        for case_id in unique_case_ids:
            report = self._reported_case_completion(case_id)
            if report is None:
                return False
            reports.append(report)
        if not reports:
            return True
        if not self._execution_port.reported_many(tuple(reports)):
            return False
        self._batch_runtime.acknowledge_case_completions(list(unique_case_ids))
        return all(
            (case := self._batch_runtime.fetch_case(case_id)) is not None
            and bool(case["completion_acknowledged"])
            for case_id in unique_case_ids
        )

    def _publish_cancelled(
        self,
        *,
        binding: CaseBinding,
        task_id: str,
        reason: str,
    ) -> bool:
        return self._execution_port.cancelled(
            binding,
            task_id,
            reason,
        )

    def acknowledge_terminal_case(
        self,
        case_id: int,
        *,
        reason: str = "",
    ) -> bool:
        case = self._batch_runtime.fetch_case(int(case_id))
        if case is None:
            return True
        status = case["status"]
        if status not in {"reported", "cancelled"}:
            return False
        if not bool(case["completion_acknowledged"]):
            if status == "reported":
                accepted = self.publish_reported_cases((int(case_id),))
            else:
                batch = self._batch_runtime.fetch_batch(int(case["batch_id"]))
                if batch is None:
                    return False
                accepted = self._publish_cancelled(
                    binding=CaseBinding(
                        execution_scope_id=batch["verification_id"],
                        program_id=batch["verification_program_id"],
                        task_id=case["verification_task_id"],
                        test_name=case["test_name"],
                    ),
                    task_id=case["task_id"],
                    reason=reason,
                )
                if accepted:
                    self._batch_runtime.acknowledge_case_completion(int(case_id))
            if not accepted:
                raise RuntimeError("verification task completion was rejected")
        self._diagnostic_publisher.flush_pending(int(case_id))
        return True
