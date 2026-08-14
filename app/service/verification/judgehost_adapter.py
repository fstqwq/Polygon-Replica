from collections.abc import Callable

from app.db import DB
from app.service.judgehost.ports.case_binding import (
    CaseArtifactBinding,
    CaseArtifactSet,
    CaseBinding,
)
from app.service.judgehost.ports.completion import (
    CaseCompletionReport,
    DiagnosticAppendResult,
)
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.task_store import VerificationTaskStore


class VerificationJudgehostAdapter:
    """Expose verification persistence through Judgehost-owned contracts."""

    def __init__(
        self,
        db: DB,
        task_store: VerificationTaskStore,
        completion_service: VerificationTaskCompletionService,
        runtime_registry: VerificationRuntimeRegistry,
    ) -> None:
        self._db = db
        self._task_store = task_store
        self._completion_service = completion_service
        self._runtime_registry = runtime_registry

    def _binding_matches(self, binding: CaseBinding) -> bool:
        row = self._task_store.runtime_row(binding.task_id)
        return bool(
            row is not None
            and row["verification_id"] == binding.execution_scope_id
            and row["program_id"] == binding.program_id
            and row["test_name"] == binding.test_name
        )

    def load_artifacts(
        self,
        execution_scope_id: str,
        selected_tests: tuple[str, ...],
        *,
        limit: int,
    ) -> CaseArtifactSet:
        if not execution_scope_id:
            return CaseArtifactSet(run_config_json="", cases=())
        with self._db.conn() as connection:
            connection.execute("BEGIN")
            verification = connection.execute(
                "SELECT run_config_json FROM verifications WHERE id=?",
                [execution_scope_id],
            ).fetchone()
            if verification is None:
                return CaseArtifactSet(run_config_json="", cases=())
            test_names = selected_tests
            if not test_names:
                test_names = tuple(
                    str(row["test_name"])
                    for row in connection.execute(
                        """
                        SELECT test_name
                        FROM verification_selected_tests
                        WHERE verification_id=?
                        ORDER BY ordinal ASC
                        LIMIT ?
                        """,
                        [execution_scope_id, max(1, int(limit))],
                    ).fetchall()
                )
            refs_by_name: dict[str, dict[str, str]] = {}
            for row in connection.execute(
                """
                SELECT test_name,role,artifact_ref
                FROM verification_task_artifacts
                WHERE verification_id=?
                  AND role IN ('generated-input','accepted-answer')
                ORDER BY task_id
                """,
                [execution_scope_id],
            ).fetchall():
                refs = refs_by_name.setdefault(
                    str(row["test_name"]),
                    {"input_ref": "", "answer_ref": ""},
                )
                key = "input_ref" if str(row["role"]) == "generated-input" else "answer_ref"
                if not refs[key]:
                    refs[key] = str(row["artifact_ref"])
            cases = tuple(
                CaseArtifactBinding(
                    test_name=test_name,
                    input_ref=refs_by_name[test_name]["input_ref"],
                    answer_ref=refs_by_name[test_name]["answer_ref"],
                )
                for test_name in test_names
                if test_name in refs_by_name
            )
            return CaseArtifactSet(
                run_config_json=str(verification["run_config_json"] or ""),
                cases=cases,
            )

    def bind_and_expose(
        self,
        bindings: tuple[CaseBinding, ...],
        *,
        run_id: str,
        judgehost_task_id: str,
        expose: Callable[[], None],
    ) -> bool:
        if not bindings:
            raise ValueError("Judgehost case bindings are required")
        first = bindings[0]
        if not first.task_id:
            raise ValueError("durable task identity is required")
        for binding in bindings:
            if (
                binding.execution_scope_id != first.execution_scope_id
                or binding.program_id != first.program_id
                or binding.task_id != first.task_id
                or binding.test_name != first.test_name
            ):
                raise ValueError("Judgehost cases do not share one durable task binding")
        return self._task_store.bind_and_expose_judgehost_runtime(
            first.task_id,
            expected_verification_id=first.execution_scope_id,
            expected_program_id=first.program_id,
            expected_test_name=first.test_name,
            run_id=run_id,
            judgehost_task_id=judgehost_task_id,
            expose=expose,
        )

    def unbind(
        self,
        task_id: str,
        *,
        judgehost_task_id: str,
    ) -> bool:
        return self._task_store.unbind_judgehost_runtime(
            task_id,
            judgehost_task_id=judgehost_task_id,
        )

    def reported_many(
        self,
        reports: tuple[CaseCompletionReport, ...],
    ) -> bool:
        return self._completion_service.reported_many(reports)

    def cancelled(
        self,
        binding: CaseBinding,
        judgehost_task_id: str,
        reason: str,
    ) -> bool:
        return self._completion_service.cancelled(
            binding,
            judgehost_task_id,
            reason,
        )

    def append(
        self,
        *,
        binding: CaseBinding,
        kind: str,
        hostname: str,
        text: str,
        received_at: str,
    ) -> DiagnosticAppendResult:
        if not self._binding_matches(binding):
            return DiagnosticAppendResult(outcome="not-applicable")
        return self._completion_service.append(
            binding=binding,
            kind=kind,
            hostname=hostname,
            text=text,
            received_at=received_at,
        )

    def case_leased(self, binding: CaseBinding) -> bool:
        if not self._binding_matches(binding):
            return False
        delivered = self._runtime_registry.case_leased(
            binding.execution_scope_id,
            binding.task_id,
        )
        if delivered:
            return True
        return self._task_store.set_task_leased(binding.task_id)
