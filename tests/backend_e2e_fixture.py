import io
from pathlib import Path

from app.main import runtime
from app.service.statement.render import statement_title_for_language
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    VerificationAdmission,
    VerificationCompileSpec,
    VerificationProgram,
    verification_task_id,
)

from tests.common import E2ETestBase


class BackendE2ETestBase(E2ETestBase):
    seed_default_workspace = True

    def _activate_verification(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int,
        signature: str = "",
        kind: str = "all",
        detail: dict[str, object] | None = None,
    ) -> str:
        admission = runtime.verification_service.admit_verification(
            VerificationAdmission(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature=signature,
                source_commit="",
                kind=kind,
            )
        )
        self.assertEqual(admission.outcome, "admitted")
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        activation = runtime.verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail=dict(detail or {}),
                programs=(
                    VerificationProgram(
                        program_id="accepted",
                        kind="main-correct",
                        source_path="fixture.cpp",
                        compile_spec=VerificationCompileSpec(
                            source_name="fixture.cpp",
                            source_file=runtime.runtime_blob_store.put_bytes(
                                b"int main(){return 0;}\n"
                            ),
                        ),
                        expected_behavior="accepted",
                    ),
                ),
                tasks=(
                    PlannedTask(
                        task_id=task_id,
                        predecessor_task_id=None,
                        task_kind="main-correct",
                        source_path="fixture.cpp",
                        program_id="accepted",
                        test_name="001.in",
                        expected_behavior="accepted",
                    ),
                ),
            )
        )
        self.assertEqual(activation.outcome, "activated")
        return task_id

    def _statement_title(
        self,
        workspace: Path,
        language: str = "english",
    ) -> str:
        return statement_title_for_language(
            workspace,
            language,
            fallback_title=Path(self.problem).name,
        )

    class _FakeUpload:
        def __init__(self, filename: str, data: bytes):
            self.filename = filename
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        async def close(self) -> None:
            self._buf.close()
