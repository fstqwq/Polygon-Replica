"""Template projections shared by verification pages, fragments and Agent YAML."""

from typing import Literal, NotRequired, TypedDict

from app.service.judgehost.callback.runpipe_transcript import RunpipeTranscriptEvent
from app.service.verification.diagnostic import TaskDiagnosticPayload
from app.service.verification.read_model import TaskCounts
from app.service.verification.types import VerificationSanityMessageRow


class RunDetailPreview(TypedDict):
    available: bool
    text: str
    truncated: bool
    limit: int
    download_verification_id: str
    download_rel_path: str
    message: str


class DiagnosticEntry(TypedDict, total=False):
    message: str
    message_truncated: bool
    message_limit: int
    level: str
    file: str
    line: int
    column: int
    can_link: bool
    file_display: str
    location_display: str
    location_title: str
    level_upper: str


class TestGenerationView(TypedDict):
    source_kind: str
    command: str
    source_path: str
    display_source: str
    status: str
    verdict: str
    status_text: str
    feedback_display: str
    error_text: str
    terminal: bool
    tone: str
    status_label: str
    table_text: str
    detail: str
    alert_severity: str
    alert_message: str
    duplicate_of: str
    skipped: bool


class RunTranscriptView(TypedDict):
    available: bool
    state: Literal["ok", "malformed", "limited", "unavailable"]
    events: list[RunpipeTranscriptEvent]
    events_shown: int
    events_total: int | None
    events_omitted: int | None
    raw_size_bytes: int
    error_offset: int | None
    error_reason: str | None
    download_verification_id: str
    download_rel_path: str
    message: str


class RunPassView(TypedDict):
    pass_number: int
    pass_label: str
    capture_status: str
    verdict_short: str
    text_tone: str
    kind: str
    time_display: str
    time_tone: str
    memory_display: str
    status_display: str
    feedback_display: str
    output_task_id: str
    input_ref: str
    output_rel: str
    transcript_rel: str
    judge_message_rel: str
    checker_log_rel: str
    feedback_rel: str
    output_preview: NotRequired[RunDetailPreview]
    input_preview: NotRequired[RunDetailPreview]
    feedback_preview: NotRequired[RunDetailPreview]
    interactive_transcript: NotRequired[RunTranscriptView]


class RunCellDetail(TypedDict):
    verdict: str
    verdict_short: str
    time_display: str
    time_tone: str
    memory_display: str
    status_display: str
    feedback_display: str
    pass_rows: list[RunPassView]
    final_row: RunPassView
    is_multi_pass: bool
    compile_error_display: str
    compile_diagnostics: list[DiagnosticEntry]
    late_diagnostics: list[TaskDiagnosticPayload]
    is_interactive: NotRequired[bool]
    mode_malformed: NotRequired[bool]


class RunCellView(TypedDict):
    text: str
    short: str
    metrics: str
    kind: str
    text_tone: str
    detail: RunCellDetail | None
    time_display: NotRequired[str]
    time_tone: NotRequired[str]
    memory_display: NotRequired[str]


class RunCaseCell(RunCellView):
    verdict: str
    time_ms: int
    memory_kb: int
    detail_available: bool


class RunColumnBase(TypedDict):
    id: str
    title: str
    mode: str
    tests_map: dict[str, RunCaseCell]


class RunFailureReason(TypedDict):
    source: str
    match_reason: str
    error: str


class RunColumn(RunColumnBase, RunFailureReason):
    artifact_verification_id: str
    source_section: str
    source_path: str
    task_kind: str
    is_main_correct_run: bool
    status: str
    created_at: str
    finished_at: str
    has_run_row: bool
    compile_log: str
    compile_diagnostics: list[DiagnosticEntry]
    error_display: str
    tests_total: int
    tests_truncated: bool
    expected_behavior: str
    expected_behavior_label: str
    expected_display: str
    expected_is_ac_only: bool
    got_short: str
    got_display: str
    result_kind: str
    result_text_tone: str
    result_tone_class: str
    expected_mismatch: bool
    matched: bool
    completed: bool
    passed_all_tests: bool
    execution_skipped: bool
    execution_skipped_reason: str
    max_time_ms: int
    max_time_display: str
    max_time_tone: str
    max_memory_kb: int
    max_memory_display: str
    failure_display: str


class RunTestNameCell(TypedDict):
    kind: str
    text: str
    short: str
    meta: str
    detail: str
    clickable: bool


class RunTestRow(TypedDict):
    index: int
    test_name: str
    display_name: str
    test_cell: RunTestNameCell
    is_placeholder: bool
    row_id: str
    cells: list[RunCellView]
    has_detail: bool
    test_source_kind: str
    test_command: str
    generation_skipped: bool
    generation_message: NotRequired[str]
    input_preview: NotRequired[RunDetailPreview]
    answer_preview: NotRequired[RunDetailPreview]
    is_interactive: NotRequired[bool]
    generate_detail: NotRequired[TestGenerationView | None]
    generation_alert: NotRequired[TestGenerationView | None]


class RunSanityTask(TypedDict):
    name: str
    label: str
    status: str
    tone: str
    detail: str
    messages: list[VerificationSanityMessageRow]


class RunSanityView(TypedDict):
    available: bool
    status: str
    reason: str
    tasks: list[RunSanityTask]
    attention_tasks: list[RunSanityTask]
    task_count: int
    ran_count: int
    checked_count: int


class RunVerificationLogs(TypedDict):
    available: bool
    title: str
    verification_id: str
    status: str
    error: str
    error_display: str
    log_rows: list[dict[str, str]]
    diagnostics: list[DiagnosticEntry]


class RunGenerationDiagnostic(TypedDict):
    title: str
    message: str


class RunTestDetailContext(TypedDict):
    verification_id: str
    detail_columns: list[RunColumnBase]
    detail_rows: list[RunTestRow]


class RunDetailContext(TypedDict):
    verification_id: str
    can_rejudge: bool
    can_cancel: bool
    detail_columns: list[RunColumn]
    detail_rows: list[RunTestRow]
    selected_program_ids: list[str]
    rerun_solution_paths: list[str]
    rerun_unavailable_reason: str
    matched_count: int
    match_total: int
    all_matched: bool
    detail_status: str
    detail_status_display: str
    detail_status_tone: str
    detail_is_main_correct_run: bool
    detail_running: bool
    detail_last_updated: str
    detail_progress_total: int
    detail_progress_reported: int
    detail_progress_placeholder_total: int
    detail_task_counts: TaskCounts
    detail_running_tasks: list[dict[str, str]]
    detail_fail_flag: bool
    detail_fail_reason: str
    detail_sanity: RunSanityView
    detail_generation_diagnostic: RunGenerationDiagnostic | None
    detail_verification_logs: RunVerificationLogs
