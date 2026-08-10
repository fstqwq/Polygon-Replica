from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import (
    CompileSubmission,
    ExecutionBatchSpec,
)
from app.service.judgehost.identity import (
    compile_key,
    domjudge_job_id,
    domjudge_submit_id,
)
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.identity import canonical_verification_id


class TestJudgehostIdentity(unittest.TestCase):
    def test_verification_id_maps_directly_to_signed_64_bit_domain(self) -> None:
        self.assertEqual(domjudge_job_id("ver-1"), 1)
        self.assertEqual(domjudge_job_id("ver-7fffffffffffffff"), (1 << 63) - 1)
        with self.assertRaisesRegex(RuntimeError, "invalid verification id"):
            domjudge_job_id("ver-8000000000000000")

    def test_verification_id_rejects_noncanonical_tokens(self) -> None:
        for token in (
            "",
            "verification",
            "ver-0",
            "ver-01",
            "ver-test",
            "ver-AB",
            "ver-1-extra",
        ):
            with self.subTest(token=token), self.assertRaisesRegex(
                RuntimeError,
                "invalid verification id",
            ):
                canonical_verification_id(token)

    def test_compile_key_contains_every_compile_input(self) -> None:
        baseline = {
            "source_hash": "1" * 64,
            "compile_hash": "2" * 32,
            "compile_config": {"toolchain_cmd_digest": "3" * 64, "script_timelimit": 30},
            "entry_point": "Main",
            "memory_limit": 262144,
        }
        expected = compile_key(**baseline)
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(compile_key(**baseline), expected)

        variants = (
            {**baseline, "source_hash": "4" * 64},
            {**baseline, "compile_hash": "5" * 32},
            {**baseline, "compile_config": {**baseline["compile_config"], "script_timelimit": 31}},
            {**baseline, "entry_point": "Other"},
            {**baseline, "memory_limit": 262145},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(compile_key(**variant), expected)

    def test_compile_key_maps_to_signed_64_bit_domain(self) -> None:
        self.assertEqual(domjudge_submit_id("0" * 64), 0)
        self.assertEqual(domjudge_submit_id(f"{(1 << 63) - 1:064x}"), (1 << 63) - 1)
        self.assertEqual(domjudge_submit_id(f"{1 << 63:064x}"), 0)
        with self.assertRaisesRegex(RuntimeError, "invalid compile key"):
            domjudge_submit_id("not-a-hash")

    def test_scheduler_ids_start_above_process_base(self) -> None:
        source = b"int main() { return 0; }\n"
        compile_identity = "8" * 64
        scheduler = BatchScheduler(id_base=123456)
        batch_id = scheduler.create_batch_with_cases(
            task_id="task-id-base",
            run_id="run-id-base",
            verification_program_id="solution-0",
            execution_signature="a" * 64,
            task_kind="solution-run",
            verification_id="ver-1",
            compile_key=compile_identity,
            compile_submission=CompileSubmission(
                compile_key=compile_identity,
                submit_id=domjudge_submit_id(compile_identity),
                source_name="main.cpp",
                source_file=PayloadFile(
                    path=Path("/tmp/test-judgehost-id-base.cpp"),
                    size=len(source),
                    identity=hashlib.sha256(source).hexdigest(),
                ),
                extra_source_items=(),
                compile_files=(),
            ),
            contest_id="local",
            mode="pass-fail",
            source_name="main.cpp",
            compile_hash="1" * 32,
            run_hash="2" * 32,
            compare_hash="3" * 32,
            source_hash="4" * 64,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=0,
            service_class="background",
            batch_spec=ExecutionBatchSpec(),
            created_at="2026-05-08T00:00:00+00:00",
            case_rows=[
                {
                    "task_id": "task-id-base",
                    "run_id": "run-id-base",
                    "test_name": "001.in",
                    "ordinal": 1,
                    "testcase_id": 1,
                    "testcase_hash": "5" * 64,
                    "testcase_input_hash": "6" * 64,
                    "testcase_answer_hash": "7" * 64,
                    "input_ref": "blob://sha256/" + ("6" * 64),
                    "answer_ref": "blob://sha256/" + ("7" * 64),
                    "status": "pending",
                }
            ],
        )
        self.assertGreater(batch_id, 123456)
        cases = scheduler.cases_for_batch(batch_id)
        self.assertEqual(len(cases), 1)
        self.assertGreater(int(cases[0]["id"]), 123456)


if __name__ == "__main__":
    unittest.main()
