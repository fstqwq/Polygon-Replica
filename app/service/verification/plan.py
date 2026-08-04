from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.service.platform.runtime_blob_store import PayloadFile


@dataclass(frozen=True)
class VerificationTestPlan:
    test_name: str
    source_kind: str
    display_source_path: str
    execution_source_name: str
    execution_source_file: PayloadFile
    execution_input_file: PayloadFile
    extra_source_files: dict[str, PayloadFile]
    tests_meta: dict[str, object]
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
    mode: str
    pass_limit: int
    run_verification_payload_base: dict[str, object]
    generate_verification_payload_base: dict[str, object]
    source_file_by_path: dict[str, PayloadFile]
    test_names: list[str]
    test_plan_by_name: dict[str, VerificationTestPlan]
    tests_meta_rows: list[dict[str, object]]
