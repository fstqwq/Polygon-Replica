from pathlib import Path

from app.service.verification.lifecycle import (
    VerificationCompileSpec,
    VerificationProgram,
)
from app.service.verification.plan import VerificationTestPlan

from tests.common import runtime


def verification_program(
    *,
    program_id: str,
    kind: str,
    source_path: str,
    expected_behavior: str,
) -> VerificationProgram:
    return VerificationProgram(
        program_id=program_id,
        kind=kind,
        source_path=source_path,
        compile_spec=VerificationCompileSpec(
            source_name=Path(source_path).name,
            source_file=runtime.runtime_blob_store.put_bytes(
                b"int main(){return 0;}\n"
            ),
        ),
        expected_behavior=expected_behavior,
    )


def task_row(
    task_id: str,
    *,
    task_kind: str,
    status: str,
    queue_index: int,
    source_path: str = "solutions/a.cpp",
    program_id: str = "solution-0",
    test_name: str = "001.in",
) -> dict[str, object]:
    return {
        "id": task_id,
        "verification_id": "ver-1",
        "predecessor_task_id": "",
        "task_kind": task_kind,
        "source_path": source_path,
        "program_id": program_id,
        "test_name": test_name,
        "expected_behavior": "accepted",
        "queue_index": queue_index,
        "status": status,
        "verdict": "",
        "run_id": "",
        "judgehost_task_id": "",
        "runtime_sec": None,
        "cpu_sec": None,
        "wall_sec": None,
        "memory_kb": None,
        "answer_correct": False,
        "compile_log": "",
        "diagnostics_json": "[]",
        "error_text": "",
        "feedback_text": "",
        "output_ref": "",
        "started_at": None,
        "finished_at": None,
        "created_at": "",
        "updated_at": "",
    }


def sanity_test_plan(
    *,
    test_name: str = "001.in",
    sample: bool = False,
    sample_output_text: str = "",
    sample_output_validate: bool = True,
) -> VerificationTestPlan:
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="manual",
        display_source_path="manual_validate.cpp",
        execution_source_name="manual_validate.cpp",
        execution_source_file=runtime.runtime_blob_store.put_bytes(
            b"int main(){return 0;}\n"
        ),
        execution_input_file=runtime.runtime_blob_store.put_bytes(b"1\n"),
        extra_source_files={},
        tests_meta={},
        sample=sample,
        sample_input_custom=False,
        sample_input_text="",
        uses_custom_sample_input=False,
        sample_output_text=sample_output_text,
        sample_output_validate=sample_output_validate,
    )
