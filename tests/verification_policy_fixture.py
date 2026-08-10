from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.verification.lifecycle import (
    VerificationCompileSpec,
    VerificationProgram,
)
from app.service.verification.plan import VerificationTestPlan


class VerificationPolicyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(
            prefix="verification-policy-test-"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime_blob_store = RuntimeBlobStore(self.root / "runtime")

    def _verification_program(
        self,
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
                source_file=self.runtime_blob_store.put_bytes(
                    b"int main(){return 0;}\n"
                ),
            ),
            expected_behavior=expected_behavior,
        )

    def _sanity_test_plan(
        self,
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
            execution_source_file=self.runtime_blob_store.put_bytes(
                b"int main(){return 0;}\n"
            ),
            execution_input_file=self.runtime_blob_store.put_bytes(b"1\n"),
            extra_source_files={},
            tests_meta={},
            sample=sample,
            sample_input_custom=False,
            sample_input_text="",
            uses_custom_sample_input=False,
            sample_output_text=sample_output_text,
            sample_output_validate=sample_output_validate,
        )
