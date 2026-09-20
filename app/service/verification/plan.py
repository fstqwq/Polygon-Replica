from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from app.service.platform.runtime_blob_store import PayloadFile, PayloadFileDescriptor
from app.service.problem.runtime_config import ProblemMode
from app.service.verification.types import VerificationTestMetadata


class VerificationProblemLimits(TypedDict):
    time_limit_ms: int
    memory_limit_mb: int
    pass_limit: int


class VerificationPayloadBase(TypedDict):
    problem_mode: ProblemMode
    run_config_json: str
    problem_limits: VerificationProblemLimits
    source_files: dict[str, PayloadFileDescriptor]


@dataclass(frozen=True)
class VerificationTestPlan:
    test_name: str
    source_kind: str
    display_source_path: str
    execution_source_name: str
    execution_source_file: PayloadFile
    execution_input_file: PayloadFile
    extra_source_files: dict[str, PayloadFile]
    tests_meta: VerificationTestMetadata
    sample: bool
    sample_input_custom: bool
    sample_input_text: str
    uses_custom_sample_input: bool
    sample_output_text: str
    sample_output_validate: bool


@dataclass(frozen=True)
class VerificationExecutionPlan:
    snapshot_root: Path
    accepted_source_path: str
    problem_mode: ProblemMode
    pass_limit: int
    run_verification_payload_base: VerificationPayloadBase
    generate_verification_payload_base: VerificationPayloadBase
    source_file_by_path: dict[str, PayloadFile]
    test_names: list[str]
    test_plan_by_name: dict[str, VerificationTestPlan]
    tests_meta_rows: list[VerificationTestMetadata]
