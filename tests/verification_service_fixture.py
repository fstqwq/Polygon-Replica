from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from app.service.judgehost.case_result import (
    CaseTerminalReport,
    build_case_terminal_report,
)
from app.service.verification.completion import (
    VerificationTaskCompletionService,
)
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
    normalize_execution_result,
)
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    VerificationAdmission,
    VerificationCompileSpec,
    VerificationProgram,
    verification_task_id,
)
from app.service.verification.service import VerificationService
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore

from tests.db_fixture import DBTestBase


_ACTIVATION_TASK_ABORT_TRIGGER = "test_abort_verification_activation_task_insert"
_COMPLETION_REF_ABORT_TRIGGER = "test_abort_verification_completion_ref_insert"


def make_execution_result(
    *,
    verdict: str,
    output_ref: str = "",
    feedback: str = "",
    error: str = "",
    compile_log: str = "",
    diagnostics: list[dict[str, object]] | None = None,
    answer_correct: bool = False,
) -> ExecutionResult:
    passes = ()
    if output_ref:
        artifact_ref = "blob://sha256/" + "a" * 64
        passes = (
            ExecutionPassResult(
                number=1,
                capture_status=CAPTURE_COMPLETE,
                runresult="correct",
                verdict=verdict,
                score_text="",
                answer_correct=answer_correct,
                usage=ExecutionUsage(0.01, 0.01, 0.01, 1),
                feedback=feedback,
                artifacts=PassArtifacts(
                    input_ref=artifact_ref,
                    output_ref=output_ref,
                    stderr_ref=artifact_ref,
                    system_ref=artifact_ref,
                    judge_message_ref=artifact_ref,
                    team_message_ref=artifact_ref,
                    metadata_ref=artifact_ref,
                    compare_metadata_ref=artifact_ref,
                ),
            ),
        )
    return normalize_execution_result(
        passes=passes,
        verdict=verdict,
        answer_correct=answer_correct,
        feedback=feedback,
        error=error,
        compile_log=compile_log,
        compile_diagnostics=() if diagnostics is None else diagnostics,
    )


def terminal_report(
    *,
    judgehost_task_id: str,
    verification_id: str,
    run_id: str,
    result: ExecutionResult,
    status: str = "ok",
    missing_case_result: bool = False,
    summary: dict[str, object] | None = None,
) -> CaseTerminalReport:
    return build_case_terminal_report(
        task_id=judgehost_task_id,
        verification_id=verification_id,
        run_id=run_id,
        status=status,
        task_status="done",
        error_text=result.outcome.error,
        summary=dict(summary or {}),
        missing_case_result=missing_case_result,
        execution_result=result,
    )


def multi_pass_result(final_output_ref: str) -> ExecutionResult:
    def pass_result(number: int, output_ref: str) -> ExecutionPassResult:
        prefix = f"blob://pass-{number}"
        return ExecutionPassResult(
            number=number,
            capture_status=CAPTURE_COMPLETE,
            runresult="correct",
            verdict="OK",
            score_text=str(number),
            answer_correct=number == 2,
            usage=ExecutionUsage(
                runtime_sec=number / 100,
                cpu_sec=number / 200,
                wall_sec=number / 50,
                memory_kb=number * 128,
            ),
            feedback=f"pass {number}",
            artifacts=PassArtifacts(
                input_ref=f"{prefix}/input",
                output_ref=output_ref,
                stderr_ref=f"{prefix}/stderr",
                system_ref=f"{prefix}/system",
                judge_message_ref=f"{prefix}/judge-message",
                team_message_ref=f"{prefix}/team-message",
                metadata_ref=f"{prefix}/metadata",
                compare_metadata_ref=f"{prefix}/compare-metadata",
            ),
        )

    return normalize_execution_result(
        passes=(
            pass_result(1, "blob://pass-1/output"),
            pass_result(2, final_output_ref),
        ),
        verdict="OK",
        answer_correct=True,
        compile_log="compiler telemetry",
        compile_diagnostics=(
            {"level": "warning", "message": "diagnostic"},
        ),
        warnings=("runner telemetry",),
    )


class _JudgehostStub:
    def resolve_artifact_blob(self, _token: str) -> bytes | None:
        return None


class VerificationServiceTestBase(DBTestBase):
    """Private SQLite fixture for verification aggregate tests."""

    def setUp(self) -> None:
        super().setUp()
        self.test_id = self._testMethodName
        self.workspace_service.ensure_problem(self.problem)
        self.workspace_service.ensure_user(self.user)
        self.workspace_service.ensure_workspace(
            self.problem,
            self.user,
            refresh_status=False,
        )
        context = self.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        self.problem_id = int(context["problem"]["id"])
        self.workspace_id = int(context["workspace"]["id"])
        self.verification_service = VerificationService(
            self.db,
            self.workspace_service,
            _JudgehostStub(),
            self.verification_task_store,
            self.runtime_blob_store,
            self.fs_manager,
            self.constants,
        )
        self.verification_task_completion_service = (
            VerificationTaskCompletionService(
                self.verification_task_store,
                self.runtime_blob_store,
                lambda _verification_id, _commit: True,
            )
        )

    @staticmethod
    def random_id(prefix: str) -> str:
        safe_prefix = str(prefix or "value").strip("-")[:24] or "value"
        return f"{safe_prefix}-{uuid.uuid4().hex[:8]}"

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
        admission = self.verification_service.admit_verification(
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
        activation = self.verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail=dict(detail or {}),
                programs=(
                    self._verification_program(
                        program_id="accepted",
                        kind="main-correct",
                        source_path="fixture.cpp",
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

    def _insert_verification_row(self, verification_id: str) -> None:
        admission = self.verification_service.admit_verification(
            VerificationAdmission(
                verification_id=verification_id,
                problem_id=self.problem_id,
                workspace_id=self.workspace_id,
                signature="",
                source_commit="",
                kind="all",
            )
        )
        self.assertEqual(admission.outcome, "admitted")

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

    def _programs_for_tasks(
        self,
        tasks: tuple[PlannedTask, ...],
    ) -> tuple[VerificationProgram, ...]:
        programs: list[VerificationProgram] = []
        by_id: dict[str, PlannedTask] = {}
        for task in tasks:
            previous = by_id.get(task.program_id)
            if previous is not None:
                if (
                    previous.task_kind != task.task_kind
                    or previous.source_path != task.source_path
                    or previous.expected_behavior != task.expected_behavior
                ):
                    raise AssertionError(
                        f"conflicting fixture program {task.program_id}"
                    )
                continue
            by_id[task.program_id] = task
            programs.append(
                self._verification_program(
                    program_id=task.program_id,
                    kind=task.task_kind,
                    source_path=task.source_path,
                    expected_behavior=task.expected_behavior,
                )
            )
        return tuple(programs)

    def _activate_graph(
        self,
        verification_id: str,
        *,
        tasks: list[dict[str, object]],
        edges: list[tuple[str, str]],
        detail: dict[str, object] | None = None,
    ) -> None:
        predecessor_by_child: dict[str, str] = {}
        for parent_id, child_id in edges:
            if child_id in predecessor_by_child:
                raise AssertionError(f"duplicate predecessor for {child_id}")
            predecessor_by_child[child_id] = parent_id
        planned = tuple(
            PlannedTask(
                task_id=str(item["id"]),
                predecessor_task_id=predecessor_by_child.get(str(item["id"])),
                task_kind=str(item.get("task_kind") or ""),
                source_path=str(item.get("source_path") or ""),
                program_id=str(item.get("program_id") or ""),
                test_name=str(item.get("test_name") or ""),
                expected_behavior=str(item.get("expected_behavior") or ""),
                result=normalize_execution_result(
                    verdict=str(item.get("verdict") or ""),
                    feedback=str(item.get("feedback_text") or ""),
                ),
            )
            for item in tasks
        )
        accepted_completion: TaskCompletion | None = None
        if not any(task.program_id == "accepted" for task in planned):
            accepted_test_name = planned[0].test_name
            accepted_task_id = verification_task_id(
                verification_id,
                "accepted",
                accepted_test_name,
            )
            planned = (
                PlannedTask(
                    task_id=accepted_task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name=accepted_test_name,
                    expected_behavior="accepted",
                ),
                *planned,
            )
            accepted_completion = TaskCompletion(
                task_id=accepted_task_id,
                status=VerificationTaskStore.TASK_DONE,
                run_id="",
                judgehost_task_id="",
                result=normalize_execution_result(verdict="OK"),
            )
        activation = self.verification_service.activate_verification(
            ActivationPlan.build(
                verification_id,
                detail=dict(detail or {}),
                programs=self._programs_for_tasks(planned),
                tasks=planned,
            )
        )
        self.assertEqual(activation.outcome, "activated")
        if accepted_completion is not None:
            self.verification_task_store.commit_task_completions(
                (accepted_completion,)
            )
        for task_index, item in enumerate(tasks):
            initial_status = str(
                item.get("status") or VerificationTaskStore.TASK_PENDING
            )
            if initial_status not in {
                VerificationTaskStore.TASK_QUEUED,
                VerificationTaskStore.TASK_LEASED,
            }:
                continue
            task_id = str(item["id"])
            bound = self.verification_task_store.bind_and_expose_judgehost_runtime(
                task_id,
                run_id=str(item.get("run_id") or f"r-test-{task_index}"),
                judgehost_task_id=str(
                    item.get("judgehost_task_id") or f"jt-{task_id}"
                ),
                expose=lambda: None,
            )
            self.assertTrue(bound)
            if initial_status == VerificationTaskStore.TASK_LEASED:
                self.verification_task_store.set_task_leased(task_id)

    def _install_activation_abort(self) -> None:
        self.db.execute(
            f"""
            CREATE TRIGGER {_ACTIVATION_TASK_ABORT_TRIGGER}
            BEFORE INSERT ON verification_tasks
            BEGIN
                SELECT RAISE(ABORT, 'forced activation task failure');
            END
            """
        )

    def _clear_activation_abort(self) -> None:
        self.db.execute(
            f"DROP TRIGGER IF EXISTS {_ACTIVATION_TASK_ABORT_TRIGGER}"
        )

    def _install_completion_ref_abort(self) -> None:
        self.db.execute(
            f"""
            CREATE TRIGGER {_COMPLETION_REF_ABORT_TRIGGER}
            BEFORE INSERT ON verification_artifact_refs
            BEGIN
                SELECT RAISE(ABORT, 'forced artifact ref failure');
            END
            """
        )

    def _clear_completion_ref_abort(self) -> None:
        self.db.execute(
            f"DROP TRIGGER IF EXISTS {_COMPLETION_REF_ABORT_TRIGGER}"
        )

    def _verification_rows(self) -> list[sqlite3.Row]:
        return self.db.fetch_all(
            "SELECT * FROM verifications ORDER BY id"
        )
