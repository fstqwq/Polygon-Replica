from collections.abc import Callable, Iterable

from app.service.judgehost.case_result import CaseTerminalReport
from app.service.judgehost.case_binding import CaseBinding
from app.service.judgehost.completion import (
    CaseCompletionReport,
    DiagnosticAppendResult,
)
from app.service.execution.model import ExecutionResult
from app.service.execution.policy import (
    execution_result_with_outcome,
    normalize_execution_result,
)
from app.service.platform.error_text import normalize_display_text
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.truncation import STORED_LOG_TRUNCATED_MARKER
from app.service.verification.result_match import verification_case_result_match
from app.service.verification.task_completion import (
    CompletionCommit,
    TaskCompletion,
)
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import VerificationStatus, VerificationTaskStatus


TASK_GENERATE_INPUT = "generate-input"
TASK_MAIN_CORRECT = "main-correct"

_ACCEPTING_VERDICTS = frozenset({"OK", "AC"})
_GENERATED_INPUT_TRUNCATION_MARKERS = (
    b"[output storage truncated after",
    STORED_LOG_TRUNCATED_MARKER,
)

PostCommitNotifier = Callable[[str, CompletionCommit], bool]


def verification_task_fail_reason(
    task_row: VerificationTaskRow,
    *,
    error_text: str,
    fallback: str = "",
) -> str:
    detail_text = normalize_display_text(error_text or fallback)
    origin_tokens = (
        task_row["task_kind"],
        task_row["source_path"],
        task_row["test_name"],
    )
    origin_text = " / ".join(token for token in origin_tokens if token)
    if origin_text and detail_text:
        return normalize_display_text(f"{origin_text}: {detail_text}")
    return normalize_display_text(origin_text or detail_text)


def _accepted_verdict(verdict: str) -> bool:
    return verdict.upper() in _ACCEPTING_VERDICTS


def _final_error(result: ExecutionResult, *, fallback: str) -> str:
    return normalize_display_text(
        result.outcome.error or result.feedback_text or fallback
    )


class VerificationTaskCompletionService:
    def __init__(
        self,
        task_store: VerificationTaskStore,
        runtime_blob_store: RuntimeBlobStore,
        post_commit_notifier: PostCommitNotifier,
    ) -> None:
        self._task_store = task_store
        self._runtime_blob_store = runtime_blob_store
        self._post_commit_notifier = post_commit_notifier

    @staticmethod
    def _report_matches_task(
        task_row: VerificationTaskRow,
        report: CaseTerminalReport,
        *,
        judgehost_task_id: str,
        verification_id: str = "",
    ) -> bool:
        row_verification_id = task_row["verification_id"]
        report_verification_id = report["verification_id"]
        return (
            report["task_id"] == judgehost_task_id
            and (not verification_id or verification_id == row_verification_id)
            and (
                not report_verification_id
                or report_verification_id == row_verification_id
            )
            and report["run_id"] == task_row["run_id"]
        )

    @staticmethod
    def _failed_completion(
        task_row: VerificationTaskRow,
        *,
        run_id: str,
        result: ExecutionResult,
        error_text: str,
        feedback_text: str | None = None,
        verdict: str | None = None,
        set_fail_reason: bool,
    ) -> TaskCompletion:
        normalized_error = normalize_display_text(error_text)
        return TaskCompletion(
            task_id=task_row["id"],
            status=VerificationTaskStatus.FAILED,
            run_id=run_id,
            judgehost_task_id=task_row["judgehost_task_id"],
            result=execution_result_with_outcome(
                result,
                verdict=(result.verdict or "FL") if verdict is None else verdict,
                error=normalized_error,
                feedback=(
                    result.feedback_text
                    if feedback_text is None
                    else normalize_display_text(feedback_text)
                ),
            ),
            fail_reason=(
                verification_task_fail_reason(
                    task_row,
                    error_text=normalized_error,
                )
                if set_fail_reason
                else ""
            ),
        )

    def _output_available(self, output_ref: str) -> bool:
        return bool(output_ref and self._runtime_blob_store.descriptor(output_ref))

    def _generated_input_truncated(self, output_ref: str) -> bool:
        tail = self._runtime_blob_store.read_tail(output_ref, max_bytes=4096)
        return any(marker in tail for marker in _GENERATED_INPUT_TRUNCATION_MARKERS)

    def _prepare_generated_input(
        self,
        task_row: VerificationTaskRow,
        *,
        run_id: str,
        result: ExecutionResult,
        report_ok: bool,
    ) -> TaskCompletion:
        test_name = task_row["test_name"]
        if not (report_ok and _accepted_verdict(result.verdict)):
            error_text = _final_error(
                result,
                fallback=f"validator rejected generated input for {test_name}",
            )
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                set_fail_reason=True,
            )
        output_ref = result.output_run_ref
        if not self._output_available(output_ref):
            error_text = f"generated input output missing for {test_name}"
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                verdict="FL",
                set_fail_reason=True,
            )
        try:
            output_truncated = self._generated_input_truncated(output_ref)
        except FileNotFoundError:
            error_text = f"generated input output missing for {test_name}"
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                verdict="FL",
                set_fail_reason=True,
            )
        if output_truncated:
            error_text = f"generated input output was truncated for {test_name}"
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                feedback_text=error_text,
                verdict="FL",
                set_fail_reason=True,
            )
        return TaskCompletion(
            task_id=task_row["id"],
            status=VerificationTaskStatus.DONE,
            run_id=run_id,
            judgehost_task_id=task_row["judgehost_task_id"],
            result=result,
            input_ref=output_ref,
        )

    def _prepare_main_correct(
        self,
        task_row: VerificationTaskRow,
        *,
        run_id: str,
        result: ExecutionResult,
        report_ok: bool,
    ) -> TaskCompletion:
        test_name = task_row["test_name"]
        if not (report_ok and _accepted_verdict(result.verdict)):
            error_text = _final_error(
                result,
                fallback=f"main correct failed on {test_name}",
            )
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                set_fail_reason=True,
            )
        output_ref = result.output_run_ref
        if not self._output_available(output_ref):
            error_text = f"main correct output missing for {test_name}"
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                verdict="FL",
                set_fail_reason=True,
            )
        return TaskCompletion(
            task_id=task_row["id"],
            status=VerificationTaskStatus.DONE,
            run_id=run_id,
            judgehost_task_id=task_row["judgehost_task_id"],
            result=result,
            answer_ref=output_ref,
        )

    def prepare(
        self,
        task_row: VerificationTaskRow,
        report: CaseTerminalReport,
    ) -> TaskCompletion:
        task_kind = task_row["task_kind"]
        test_name = task_row["test_name"]
        run_id = report["run_id"] or task_row["run_id"]
        result = report["execution_result"]
        report_ok = report["status"] == VerificationStatus.OK.value

        if report["missing_case_result"]:
            error_text = _final_error(
                result,
                fallback=f"judgehost case result missing for {test_name}",
            )
            return self._failed_completion(
                task_row,
                run_id=run_id,
                result=result,
                error_text=error_text,
                set_fail_reason=True,
            )

        if task_kind == TASK_GENERATE_INPUT:
            return self._prepare_generated_input(
                task_row,
                run_id=run_id,
                result=result,
                report_ok=report_ok,
            )
        if task_kind == TASK_MAIN_CORRECT:
            return self._prepare_main_correct(
                task_row,
                run_id=run_id,
                result=result,
                report_ok=report_ok,
            )
        matched, completed, _observed_pass, mismatch_reason = (
            verification_case_result_match(
                task_row["expected_behavior"],
                result,
            )
        )
        if matched:
            return TaskCompletion(
                task_id=task_row["id"],
                status=VerificationTaskStatus.DONE,
                run_id=run_id,
                judgehost_task_id=task_row["judgehost_task_id"],
                result=result,
            )
        error_text = (
            normalize_display_text(mismatch_reason)
            if mismatch_reason
            else _final_error(
                result,
                fallback=(
                    "solution result did not match expected behavior"
                    if completed
                    else "solution execution did not produce a complete result"
                ),
            )
        )
        return self._failed_completion(
            task_row,
            run_id=run_id,
            result=result,
            error_text=error_text,
            set_fail_reason=True,
        )

    def commit(
        self,
        completions: Iterable[TaskCompletion],
        *,
        notify: bool = True,
    ) -> CompletionCommit:
        prepared = tuple(completions)
        if not prepared:
            raise ValueError("at least one task completion is required")
        committed = self._task_store.commit_task_completions(prepared)
        if notify:
            self._post_commit_notifier(committed.verification_id, committed)
        return committed

    def reported_many(
        self,
        reports: tuple[CaseCompletionReport, ...],
    ) -> bool:
        completions: list[TaskCompletion] = []
        verification_ids: set[str] = set()
        task_ids: set[str] = set()
        for candidate in reports:
            binding = candidate.binding
            verification_task_id = binding.task_id
            if not verification_task_id:
                continue
            task_row = self._task_store.runtime_row(verification_task_id)
            if task_row is None:
                return False
            if (
                task_row["test_name"] != binding.test_name
                or task_row["program_id"] != binding.program_id
                or verification_task_id in task_ids
                or not self._report_matches_task(
                    task_row,
                    candidate.report,
                    judgehost_task_id=candidate.judgehost_task_id,
                    verification_id=binding.execution_scope_id,
                )
            ):
                return False
            task_ids.add(verification_task_id)
            verification_ids.add(task_row["verification_id"])
            completions.append(self.prepare(task_row, candidate.report))
        if len(verification_ids) > 1:
            return False
        if completions:
            self.commit(completions)
        return True

    def cancelled(
        self,
        binding: CaseBinding,
        judgehost_task_id: str,
        reason: str,
    ) -> bool:
        task_row = self._task_store.runtime_row(binding.task_id)
        if task_row is None:
            return True
        if (
            task_row["verification_id"] != binding.execution_scope_id
            or task_row["program_id"] != binding.program_id
            or task_row["test_name"] != binding.test_name
            or task_row["judgehost_task_id"] != judgehost_task_id
        ):
            return False
        cancel_reason = normalize_display_text(
            reason or "verification cancelled by user"
        )
        self.commit(
            (
                TaskCompletion(
                    task_id=task_row["id"],
                    status=VerificationTaskStatus.CANCELLED,
                    run_id=task_row["run_id"],
                    judgehost_task_id=task_row["judgehost_task_id"],
                    result=normalize_execution_result(error=cancel_reason),
                    fail_reason=cancel_reason,
                ),
            )
        )
        return True

    def append(
        self,
        *,
        binding: CaseBinding,
        kind: str,
        hostname: str,
        text: str,
        received_at: str,
    ) -> DiagnosticAppendResult:
        return DiagnosticAppendResult(
            outcome=self._task_store.append_diagnostic(
                task_id=binding.task_id,
                kind=kind,
                hostname=hostname,
                text=text,
                received_at=received_at,
            )
        )
