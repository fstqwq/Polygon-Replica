from dataclasses import dataclass

from app.service.judgehost.batch.model import CompileSubmission, ExecutionBatchSpec
from app.service.platform.runtime_blob_store import PayloadFile


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

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "answer_name": self.answer_name,
            "input_file": self.input_file.to_payload(),
            "answer_file": self.answer_file.to_payload(),
        }
