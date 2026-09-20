import time
from dataclasses import replace
from pathlib import Path

from app.service.execution.policy import normalize_execution_result
from app.service.judgehost.batch.model import ExecutionBatchRow
from app.service.judgehost.domjudge.wire_model import DomjudgeWork
from app.service.problem.build_config import dumps_build_config
from app.service.verification.execution import VerificationExecutionCallbacks
from app.service.verification.lifecycle import verification_task_id
from app.service.verification.plan import VerificationPayloadBase, VerificationTestPlan
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_scheduler import TaskPublishResult
from app.service.verification.types import VerificationTaskStatus
from app.service.verification.workflow import TaskExecutionContext, _publish_task
from app.service.verification.workflow_policy import build_graph

from tests.common import E2ETestBase, override_config_values, runtime
from tests.db_helpers import activate_test_verification, admit_test_verification
from tests.identity_helpers import canonical_test_verification_id

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


class VerificationExecutionTestBase(E2ETestBase):
    def setUp(self) -> None:
        super().setUp()
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)

    @staticmethod
    def _payload_base() -> VerificationPayloadBase:
        return {
            "problem_mode": "pass-fail", "run_config_json": "{}",
            "problem_limits": {"time_limit_ms": 2000, "memory_limit_mb": 1024, "pass_limit": 1},
            "source_files": {},
        }

    def _execution(
        self, *, test_names: tuple[str, ...] = ("001.in",),
        bypass_case_result_cache: bool = True, unique_inputs: bool = False,
        sanity_status: str = "pending",
    ) -> TaskExecutionContext:
        verification_id = canonical_test_verification_id(self.random_id("sanity-real"))
        workspace = self._workspace_path()
        (workspace / "solutions/std.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (workspace / "config/build.json").write_text(
            dumps_build_config({"generator_sources": [], "accepted_solution_source": "solutions/std.cpp"}),
            encoding="utf-8",
        )
        plans = {name: sanity_test_plan(test_name=name) for name in test_names}
        if unique_inputs:
            plans = {name: replace(plan, execution_input_file=runtime.runtime_blob_store.put_bytes(name.encode()))
                     for name, plan in plans.items()}
        source = runtime.runtime_blob_store.put_bytes(b"int main(){return 0;}\n")
        graph = build_graph(
            verification_id=verification_id, accepted_source_path="solutions/std.cpp",
            source_file_by_path={"solutions/std.cpp": source},
            test_plan_by_name=plans, targets=[], test_names=list(test_names),
        )
        context = runtime.workspace_service.workspace_context(self.problem, self.user)
        admit_test_verification(
            verification_id=verification_id, problem_id=context["problem"]["id"],
            workspace_id=context["workspace"]["id"],
        )
        activate_test_verification(
            verification_id, programs=graph.programs, tasks=graph.tasks,
            detail={"mode": "pass-fail", "sanity_status": sanity_status, "run_config_json": "{}"},
        )
        return TaskExecutionContext(
            problem=self.problem, user=self.user, verification_id=verification_id,
            problem_mode="pass-fail", pass_limit=1, snapshot_root=workspace,
            artifact_file_by_test_ref={}, program_by_id={program.program_id: program for program in graph.programs},
            execution_template_by_program_id={}, test_plan_by_name=plans,
            run_verification_payload_base=self._payload_base(), generate_verification_payload_base=self._payload_base(),
            bypass_case_result_cache=bypass_case_result_cache, service_class="foreground", judgehost=runtime.judgehost_task_service,
            runtime_blob_store=runtime.runtime_blob_store, verification_service=runtime.verification_service,
            task_store=runtime.verification_task_store,
        )

    def _seed_sanity(self) -> tuple[str, Path]:
        execution = self._execution()
        input_ref = runtime.runtime_blob_store.put_bytes(b"1\n").blob_ref
        for program, artifact_refs in (("generator-0", {"input_ref": input_ref}), ("accepted", {"answer_ref": input_ref})):
            runtime.verification_task_store.commit_task_completions((TaskCompletion(
                task_id=verification_task_id(execution.verification_id, program, "001.in"),
                status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="",
                result=normalize_execution_result(verdict="OK"), **artifact_refs,
            ),))
        return execution.verification_id, runtime.storage_layout.prepare_verification_layout(execution.verification_id).logs

    @staticmethod
    def _source_name(work: DomjudgeWork) -> str:
        return runtime.judgehost_task_service.domjudge_get_source_files(work["submitid"], work["contestid"])[0].filename

    def _assert_closed_batches(self, verification_id: str, sources: set[str]) -> list[ExecutionBatchRow]:
        service = runtime.judgehost_task_service
        batch_ids: set[int] = set()
        for run_id in service.problem_run_ids(self.problem):
            task = service.task_snapshot_for_run(run_id)
            assert task is not None
            if task["verification_id"] != verification_id:
                continue
            service.wait_for_task_result(task["id"], timeout_sec=5)
            terminal = service.task_snapshot_for_run(run_id)
            assert terminal is not None
            self.assertIn(terminal["status"], {"completed", "failed"})
            for case in service.run_case_snapshots(run_id):
                self.assertTrue(case["completion_acknowledged"])
                batch_ids.add(case["batch_id"])
        batches: list[ExecutionBatchRow] = []
        for batch_id in batch_ids:
            deadline = time.monotonic() + 5
            batch = service.batch_snapshot(batch_id)
            assert batch is not None
            while batch["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
                batch = service.batch_snapshot(batch_id)
                assert batch is not None
            self.assertIn(batch["status"], {"completed", "failed"})
            batches.append(batch)
        self.assertEqual({batch["source_name"] for batch in batches}, sources)
        return batches

    @staticmethod
    def _execution_callbacks(execution: TaskExecutionContext) -> VerificationExecutionCallbacks:
        service = runtime.judgehost_task_service
        return VerificationExecutionCallbacks(
            publish_task=lambda row: _publish_task(row, execution=execution),
            probe_task_case_cache=service.probe_task_case_cache,
            close_programs=lambda programs: service.close_programs(execution.verification_id, programs),
            reconcile_expired_leases=lambda: service.reconcile_expired_verification_leases(execution.verification_id),
            finish_tasks=service.finish_reported_tasks,
        )

    def _run_execution(self, execution: TaskExecutionContext) -> None:
        rows = runtime.verification_task_store.list_rows(execution.verification_id)
        runtime.verification_execution_service.run(
            execution.verification_id,
            callbacks=self._execution_callbacks(execution),
            edges=[(row["predecessor_task_id"], row["id"]) for row in rows if row["predecessor_task_id"]],
        )

    def _publish_generator(self, execution: TaskExecutionContext) -> TaskPublishResult:
        row = next(row for row in runtime.verification_task_store.list_rows(execution.verification_id)
                   if row["task_kind"] == "generate-input")
        published = _publish_task(row, execution=execution)
        self.assertIsNone(published.terminal_result)
        return published

    def _assert_durable_terminal(self, verification_id: str, expected_status: str) -> None:
        snapshot = runtime.verification_service.verification_snapshot(verification_id)
        assert snapshot is not None
        self.assertEqual(snapshot["record"]["status"], expected_status)
        self.assertTrue(snapshot["record"]["finished_at"])
        self.assertTrue(all(row["status"] in {VerificationTaskStatus.CANCELLED, VerificationTaskStatus.FAILED}
                            for row in snapshot["tasks"]))
