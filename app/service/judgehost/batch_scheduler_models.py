from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True)
class CompileSubmission:
    compile_key: str
    submit_id: int
    source_name: str
    source_bytes: bytes
    extra_source_items: tuple[tuple[str, bytes], ...]
    compile_files: tuple[tuple[str, bytes, bool], ...]


@dataclass(frozen=True)
class ExecutionBatchSpec:
    run_files: tuple[tuple[str, bytes, bool], ...] = ()
    compare_files: tuple[tuple[str, bytes, bool], ...] = ()


@dataclass(frozen=True)
class CaseResult:
    runresult: str
    verdict: str
    runtime_sec: float
    cpu_sec: float
    wall_sec: float
    memory_kb: int
    score_text: str
    output_run_rel: str
    output_error_rel: str
    output_system_rel: str
    output_diff_rel: str
    metadata_rel: str
    compare_metadata_rel: str
    team_message_rel: str
    feedback_text: str
    feedback_files: tuple[str, ...]
    answer_correct: bool
    test_row_json: str


class ExecutionBatchRow(TypedDict):
    batch_id: int
    task_id: str
    run_id: str
    group_key: str
    verification_id: str
    domjudge_job_id: int
    compile_key: str
    contest_id: str
    mode: str
    source_name: str
    source_path: str
    work_root: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    source_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    expected_behavior: str
    verification_source: str
    bypass_case_result_cache: int
    compile_success: int | None
    compile_state: str
    compile_owner: str
    materialization_state: str
    service_class: str
    compile_output_b64: str
    compile_metadata_b64: str
    debug_text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str


class JudgehostCaseRow(TypedDict):
    id: int
    batch_id: int
    task_id: str
    run_id: str
    test_name: str
    ordinal: int
    scope_sequence: int
    testcase_id: int | None
    testcase_hash: str
    testcase_input_hash: str
    testcase_answer_hash: str
    input_ref: str
    answer_ref: str
    status: str
    lease_owner: str
    runresult: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    output_run_rel: str
    output_error_rel: str
    output_system_rel: str
    output_diff_rel: str
    metadata_rel: str
    compare_metadata_rel: str
    team_message_rel: str
    score_text: str
    debug_text: str
    verification_published: bool
    created_at: str
    updated_at: str


class ExecutionBatchAppendResult(TypedDict):
    batch_id: int
    outcome: str
    inserted: int


class ExecutionBatchFinalizationClaim(TypedDict):
    batch: ExecutionBatchRow
    cases: list[JudgehostCaseRow]


@dataclass
class ExecutionBatchRecord:
    batch_id: int
    task_id: str
    run_id: str
    group_key: str
    verification_id: str
    domjudge_job_id: int
    compile_key: str
    contest_id: str
    mode: str
    source_name: str
    source_path: str
    work_root: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    source_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    expected_behavior: str
    verification_source: str
    bypass_case_result_cache: int
    compile_success: int | None
    compile_state: str
    compile_owner: str | None
    materialization_state: str
    service_class: str
    has_been_claimed: bool
    admission_sequence: int
    generation: int
    compile_output_b64: str | None
    compile_metadata_b64: str | None
    debug_text: str
    status: str
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass
class CaseRecord:
    id: int
    batch_id: int
    task_id: str
    run_id: str
    test_name: str
    ordinal: int
    scope_sequence: int
    heap_generation: int
    testcase_id: int | None
    testcase_hash: str
    testcase_input_hash: str
    testcase_answer_hash: str
    input_ref: str
    answer_ref: str
    status: str
    lease_owner: str | None
    result: CaseResult | None
    debug_text: str
    verification_published: bool
    cancel_requested: bool
    terminal_result: CaseResult | None
    requeue_on_abort: bool
    claim_generation: int
    created_at: str
    updated_at: str


@dataclass
class StatusCounts:
    staged: int = 0
    cache_pending: int = 0
    cache_probing: int = 0
    pending: int = 0
    leased: int = 0
    reporting: int = 0
    reported: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return (
            self.staged
            + self.cache_pending
            + self.cache_probing
            + self.pending
            + self.leased
            + self.reporting
            + self.reported
            + self.cancelled
        )

    @property
    def terminal(self) -> int:
        return self.reported + self.cancelled


@dataclass
class TaskCaseCounts:
    total: int = 0
    remaining: int = 0


@dataclass(frozen=True)
class CaseClaim:
    case_id: int
    generation: int
    batch_id: int
    task_id: str
    test_name: str


class CaseClaimBusy(RuntimeError):
    """A duplicate callback arrived while the first callback still owns the Case."""
