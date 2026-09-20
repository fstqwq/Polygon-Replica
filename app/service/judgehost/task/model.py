from dataclasses import dataclass
from collections.abc import Mapping
from typing import TypedDict

from app.service.judgehost.batch.model import CompileSubmission, ExecutionBatchSpec
from app.service.judgehost.domjudge.wire_model import (
    DomjudgeCompileConfig,
    DomjudgeCompareConfig,
    DomjudgeRunConfig,
)
from app.service.platform.runtime_blob_store import PayloadFile, PayloadFileDescriptor


class ExecutionTemplatePayload(TypedDict):
    compile_key: str
    source_hash: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    toolchain_cmd_digest: str
    compile_config: DomjudgeCompileConfig
    run_config: DomjudgeRunConfig
    compare_config: DomjudgeCompareConfig
    compile_files: list[tuple[str, bytes, bool]]
    run_files: list[tuple[str, bytes, bool]]
    compare_files: list[tuple[str, bytes, bool]]
    main_correct: bool


class PreparedTestPayload(TypedDict):
    name: str
    answer_name: str
    input_file: PayloadFileDescriptor
    answer_file: PayloadFileDescriptor


class CollectedVerificationPayload(TypedDict):
    problem_mode: str
    tests: list[PreparedTestPayload]
    run_config_json: str
    problem_limits: dict[str, int]
    source_files: dict[str, PayloadFileDescriptor]


@dataclass(frozen=True, slots=True)
class ExecutionTemplate:
    """Program preparation shared by cases; config JSON and file tuples are immutable."""

    submission: CompileSubmission
    batch_spec: ExecutionBatchSpec
    upload_filename: str
    entry_point: str
    source_hash: str
    compile_hash: str
    run_hash: str
    compare_hash: str
    compile_config_json: str
    run_config_json: str
    compare_config_json: str
    pass_limit: int
    policy: tuple[str, str, str, bool]
    execution_signature: str


@dataclass(frozen=True, slots=True)
class PreparedTest:
    """An input/answer pair fixed in runtime storage, independent of the program."""

    name: str
    answer_name: str
    input_file: PayloadFile
    answer_file: PayloadFile
    testcase_hash: str
    testcase_id: int

    def to_payload(self) -> PreparedTestPayload:
        return {
            "name": self.name,
            "answer_name": self.answer_name,
            "input_file": self.input_file.to_payload(),
            "answer_file": self.answer_file.to_payload(),
        }


class TaskPayload(TypedDict, total=False):
    """Known task fields across template preparation, admission and retention.

    Verification overrides remain raw until each source, limit and testcase is
    validated by preparation. Retention drops precomputed and source-file data.
    """

    type: str
    run_id: str
    problem: str
    username: str
    artifact_verification_id: str
    submission_path: str
    source_name: str
    source_label: str
    source_file: PayloadFileDescriptor
    entry_point: str
    selected_tests: list[str]
    verification_id: str
    verification_task_id: str
    verification_program_id: str
    expected_behavior: str
    verification_source: str
    task_kind: str
    bypass_case_result_cache: bool
    compile_only: bool
    verification_payload: Mapping[str, object]
    enqueued_at: str
    mode: str
    service_class: str
    extra_source_files: dict[str, PayloadFileDescriptor]
    manual_validate_only: bool
    precomputed: ExecutionTemplate
    execution_signature: str
