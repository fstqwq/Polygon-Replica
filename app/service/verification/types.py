from enum import StrEnum
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from app.service.execution.model import ExecutionResult, JsonValue
from app.service.execution.test_rows import ExecutionTestRow
from app.service.verification.diagnostic import TaskDiagnosticDisplay, TaskDiagnosticPayload


class Kind(StrEnum):
    ALL = "all"
    PACKAGE = "package"
    SAMPLE = "sample"
    CUSTOM = "custom"


class VerificationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationTarget(TypedDict):
    path: str
    expected_behavior: str
    program_id: str
    upload_filename: NotRequired[str]
    upload_content: NotRequired[bytes]


class VerificationTaskContext(TypedDict):
    """Immutable task metadata and the identities of its current execution."""

    id: str
    verification_id: str
    task_kind: str
    source_path: str
    program_id: str
    test_name: str
    expected_behavior: str
    run_id: str
    judgehost_task_id: str


class VerificationTaskRow(VerificationTaskContext):
    predecessor_task_id: str
    queue_index: int
    status: VerificationTaskStatus
    result: ExecutionResult
    result_json: str
    verdict: str
    runtime_sec: float | None
    cpu_sec: float | None
    wall_sec: float | None
    memory_kb: int | None
    answer_correct: bool
    compile_log: str
    error_text: str
    feedback_text: str
    output_ref: str
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str
    input_ref: NotRequired[str]
    answer_ref: NotRequired[str]
    late_diagnostics: NotRequired[list[TaskDiagnosticPayload]]
    late_diagnostic_text: NotRequired[str]
    diagnostic_display: NotRequired[TaskDiagnosticDisplay]


class VerificationTaskReadRow(TypedDict):
    id: str
    task_kind: str
    source_path: str
    program_id: str
    test_name: str
    status: VerificationTaskStatus


ACTIVE = frozenset(
    (VerificationStatus.QUEUED.value, VerificationStatus.RUNNING.value)
)


class WorkspaceVerificationRow(TypedDict):
    id: str
    status: VerificationStatus
    signature: str
    source_commit: str
    kind: str
    fail_reason: str
    error: str
    sanity_status: str
    created_at: str
    finished_at: str


class VerificationRecordRow(WorkspaceVerificationRow):
    problem_id: int
    workspace_id: int | None


class VerificationTestMetadata(TypedDict, total=False):
    index: int
    test_name: str
    kind: str
    id: str
    sample: bool
    sample_input_custom: bool
    sample_output_custom: bool
    sample_output_validate: bool
    desc: str
    source: str
    command: str
    payload_source: str


class VerificationSanityMessageRow(TypedDict):
    severity: str
    test_name: str
    message: str


class VerificationSanityCheckRow(TypedDict):
    name: str
    status: str
    checked_count: int
    messages: list[VerificationSanityMessageRow]


class VerificationDetail(TypedDict, total=False):
    """Persisted metadata; a missing verification is represented by an empty row."""

    mode: str
    pass_limit: int
    run_config_json: str
    error: str
    failed_step: str
    failed_check: str
    failed_test: str
    sanity_status: str
    sanity_checked_count: int
    validation_status: str
    validated_count: int
    selected_test_names: list[str]
    source_paths: list[str]
    sanity_checks: list[str]
    sanity_check_results: list[VerificationSanityCheckRow]
    tests_meta_rows: list[VerificationTestMetadata]


class VerificationDetailEnvelope(TypedDict):
    id: str
    status: VerificationStatus
    details: VerificationDetail


class VerificationProgramUsage(TypedDict):
    tests: int
    time_ms_total: int
    time_user_ms_total: int
    time_wall_ms_total: int
    memory_kb_peak: int


class VerificationCaseTestRow(ExecutionTestRow):
    late_diagnostics: list[TaskDiagnosticPayload]
    late_diagnostic_text: str


class VerificationProgramSummaryFields(TypedDict):
    mode: str
    source: str
    task_kind: str
    tests_total: int
    compile_diagnostics: list[dict[str, JsonValue]]
    error: str
    expected_behavior: NotRequired[str]
    tests_skipped: NotRequired[int]


class VerificationProgramSummary(VerificationProgramSummaryFields):
    tests: Sequence[ExecutionTestRow]
    compile_log: str
    usage: VerificationProgramUsage
    artifact_verification_id: NotRequired[str]
    pass_limit: NotRequired[int]
    selected_tests: NotRequired[list[str]]
    selected_tests_count: NotRequired[int]
    skipped_tests: NotRequired[int]
    run_config: NotRequired[dict[str, object]]
    cancelled: NotRequired[bool]


class VerificationProgramDetailSummary(VerificationProgramSummaryFields):
    tests: Sequence[VerificationCaseTestRow]


class VerificationRuntimeColumn(TypedDict):
    source: str
    summary: VerificationProgramSummary
    summary_has_tl: bool


WorkspaceVerificationKey = tuple[int, int]
