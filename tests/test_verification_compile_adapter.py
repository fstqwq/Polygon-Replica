from pathlib import Path
from unittest.mock import patch

from app.impl.problem.compile_check import judgehost_compile_check_error
from app.main import runtime

from tests.common import E2ETestBase


class TestVerificationCompileAdapter(E2ETestBase):
    def test_judgehost_compile_check_reads_full_diagnostics_from_transient_task_result(self) -> None:
        with (
            patch.object(runtime.judgehost_task_service, "enabled", return_value=True),
            patch.object(runtime.judgehost_task_service, "auth_token_configured", return_value=True),
            patch.object(runtime.judgehost_task_service, "status", return_value={"hosts_online": 1}),
            patch.object(
                runtime.judgehost_task_service,
                "compile_only_submission",
                return_value={
                    "status": "failed",
                    "error": "Compiling failed with exitcode 1, compiler output:",
                    "summary": {
                        "error": "Compiling failed with exitcode 1, compiler output:",
                        "compile_diagnostics": [
                            {
                                "message": "Compiling failed with exitcode 1, compiler output:\nvalidator.cpp:4:35: error: expected ';' before 'inf'"
                            }
                        ],
                    },
                },
            ),
            patch("app.impl.problem.compile_check.workspace_testlib_header", return_value=None),
        ):
            msg = judgehost_compile_check_error(
                application_runtime=runtime,
                problem=self.problem,
                user=self.user,
                workspace=Path("."),
                source_path="validators/validator.cpp",
                source_content="int main(){\n",
                verification_source="problem.validator.save_source",
            )
        self.assertIn("validator.cpp:4:35: error: expected ';' before 'inf'", msg)

    def test_judgehost_compile_check_surfaces_backend_failure_when_result_is_missing(self) -> None:
        with (
            patch.object(runtime.judgehost_task_service, "enabled", return_value=True),
            patch.object(runtime.judgehost_task_service, "auth_token_configured", return_value=True),
            patch.object(runtime.judgehost_task_service, "status", return_value={"hosts_online": 1}),
            patch.object(
                runtime.judgehost_task_service,
                "compile_only_submission",
                side_effect=RuntimeError("Compiling failed with exitcode 1, compiler output:"),
            ),
            patch("app.impl.problem.compile_check.workspace_testlib_header", return_value=None),
        ):
            msg = judgehost_compile_check_error(
                application_runtime=runtime,
                problem=self.problem,
                user=self.user,
                workspace=Path("."),
                source_path="validators/validator.cpp",
                source_content="int main(){\n",
                verification_source="problem.validator.save_source",
            )
        self.assertIn("Compiling failed with exitcode 1, compiler output:", msg)
