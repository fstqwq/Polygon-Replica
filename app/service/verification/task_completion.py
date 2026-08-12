from dataclasses import dataclass

from app.service.execution.codec import compile_diagnostics_payload
from app.service.execution.model import ExecutionResult
from app.service.platform.hashing import canonical_json
from app.service.verification.lifecycle import ParentTransition
from app.service.verification.types import VerificationTaskStatus


@dataclass(frozen=True)
class TaskCompletion:
    task_id: str
    status: VerificationTaskStatus
    run_id: str
    judgehost_task_id: str
    result: ExecutionResult
    input_ref: str = ""
    answer_ref: str = ""
    fail_reason: str = ""

    @property
    def verdict(self) -> str:
        return self.result.verdict

    @property
    def runtime_sec(self) -> float | None:
        return self.result.runtime_sec

    @property
    def cpu_sec(self) -> float | None:
        return self.result.cpu_sec

    @property
    def wall_sec(self) -> float | None:
        return self.result.wall_sec

    @property
    def memory_kb(self) -> int | None:
        return self.result.memory_kb

    @property
    def compile_log(self) -> str:
        return self.result.compile.log

    @property
    def diagnostics_json(self) -> str:
        return canonical_json(
            compile_diagnostics_payload(self.result.compile.diagnostics),
            ensure_ascii=False,
        )

    @property
    def error_text(self) -> str:
        return self.result.outcome.error

    @property
    def feedback_text(self) -> str:
        return self.result.feedback_text

    @property
    def output_ref(self) -> str:
        return self.result.output_run_ref

    @property
    def answer_correct(self) -> bool:
        return self.result.answer_correct


@dataclass(frozen=True)
class CompletionCommit:
    verification_id: str
    effective_completions: tuple[TaskCompletion, ...]
    committed_task_ids: frozenset[str]
    already_terminal_task_ids: frozenset[str]
    skipped_task_ids: frozenset[str]
    cancelled_task_ids: frozenset[str] = frozenset()
    parent_transition: ParentTransition = ""
    sanity_claimed: bool = False
    failure_reason: str = ""
