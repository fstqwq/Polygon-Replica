from typing import NotRequired, TypedDict

from app.service.execution.model import JsonValue
from app.service.execution.test_rows import ExecutionTestRow


class TaskUsageSummary(TypedDict, total=False):
    tests: int
    time_ms_total: int
    time_user_ms_total: int
    time_wall_ms_total: int
    memory_kb_peak: int


class TaskScriptHashes(TypedDict):
    compile: str
    run: str
    compare: str


class TaskHostSummary(TypedDict, total=False):
    task_id: str
    hostname: str
    status: str
    script_hashes: TaskScriptHashes


class TaskSummary(TypedDict, total=False):
    mode: str
    pass_limit: int
    source: str
    selected_tests: list[str]
    selected_tests_count: int
    verification_source: str
    task_kind: str
    tests: list[ExecutionTestRow]
    compile_log: str
    compile_diagnostics: list[dict[str, JsonValue]]
    toolchain_digest: str
    limits: dict[str, int]
    usage: TaskUsageSummary
    judgehost: TaskHostSummary
    compile_only: bool
    error: str
    status: str
    warnings: list[str]


class TaskStoredResult(TypedDict, total=False):
    run_status: str
    error: str
    summary: TaskSummary
    cancelled: bool
    reason: str


class TaskFinalizationPayload(TypedDict):
    run_status: str
    summary: TaskSummary
    error: NotRequired[str]
