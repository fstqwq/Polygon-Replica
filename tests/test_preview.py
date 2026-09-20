import subprocess
import sys
from dataclasses import replace

from app.service.execution.model import CAPTURE_COMPLETE, ExecutionPassResult, ExecutionUsage, PassArtifacts
from app.service.execution.policy import normalize_execution_result
from app.service.judgehost.domjudge.wire_model import DomjudgeWork
from app.service.problem.build_config import dumps_build_config
from app.service.verification.plan import VerificationTestPlan
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus

from tests.backend_e2e_fixture import BackendE2ETestBase
from tests.common import override_config_values, runtime
from tests.db_helpers import activate_test_verification, admit_test_verification, verification_programs_for_tasks
from tests.identity_helpers import canonical_test_verification_id
from tests.judgehost_support import JudgehostReply, reporting_judgehost


class TestPreview(BackendE2ETestBase):
    def setUp(self) -> None:
        super().setUp()
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)

    def _plan(self, test_name: str, *, custom: bool) -> VerificationTestPlan:
        return VerificationTestPlan(
            test_name=test_name,
            source_kind="manual",
            display_source_path="manual_validate.cpp",
            execution_source_name="manual_validate.cpp",
            execution_source_file=runtime.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
            execution_input_file=runtime.runtime_blob_store.put_bytes(b"base\n"),
            extra_source_files={}, tests_meta={}, sample=True,
            sample_input_custom=custom,
            sample_input_text=f"custom-{test_name}\n" if custom else "",
            uses_custom_sample_input=False,
            sample_output_text=f"answer-custom-{test_name}\n" if custom else "ok\n",
            sample_output_validate=True,
        )

    @staticmethod
    def _execute(work: DomjudgeWork) -> tuple[str, bytes, bytes, JudgehostReply]:
        service = runtime.judgehost_task_service
        source = service.domjudge_get_source_files(work["submitid"], work["contestid"])[0]
        files = {
            row.filename: runtime.runtime_blob_store.read(row.payload)
            for row in service.domjudge_get_testcase_files(int(work["testcase_id"]))
        }
        output = subprocess.run(
            [sys.executable, "-c", runtime.runtime_blob_store.read(source.payload).decode("utf-8")],
            input=files["input"], capture_output=True, check=True, timeout=10,
        ).stdout
        verdict = "correct" if source.filename == "std.py" or output == files["output"] else "wrong-answer"
        return source.filename, files["input"], files["output"], JudgehostReply(output=output, runresult=verdict)

    def test_authored_sample_output_is_checked_against_persisted_answer_without_checker(self) -> None:
        verification_id = canonical_test_verification_id(self.random_id("sample-check"))
        workspace = self._workspace_path()
        (workspace / "solutions/std.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "config/build.json").write_text(
            dumps_build_config({"generator_sources": [], "accepted_solution_source": "solutions/std.py"}),
            encoding="utf-8",
        )
        context = runtime.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        admit_test_verification(
            verification_id=verification_id, problem_id=context["problem"]["id"],
            workspace_id=context["workspace"]["id"],
        )
        generator_id = verification_task_id(verification_id, "generator-0", "001.in")
        task_id = verification_task_id(verification_id, "accepted", "001.in")
        tasks = (
            PlannedTask(task_id=generator_id, predecessor_task_id=None, task_kind="generate-input",
                        source_path="manual_validate.cpp", program_id="generator-0", test_name="001.in", expected_behavior="accepted"),
            PlannedTask(task_id=task_id, predecessor_task_id=generator_id, task_kind="main-correct",
                        source_path="solutions/std.py", program_id="accepted", test_name="001.in", expected_behavior="accepted"),
        )
        activate_test_verification(
            verification_id, tasks=tasks, programs=verification_programs_for_tasks(tasks),
            detail={"mode": "pass-fail", "sanity_status": "pending"},
        )
        empty = runtime.runtime_blob_store.put_bytes(b"").blob_ref
        input_ref = runtime.runtime_blob_store.put_bytes(b"base\n").blob_ref
        answer_ref = runtime.runtime_blob_store.put_bytes(b"ok\n").blob_ref
        runtime.verification_task_store.commit_task_completions((TaskCompletion(
            task_id=generator_id, status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="",
            result=normalize_execution_result(verdict="OK"), input_ref=input_ref,
        ),))
        runtime.verification_task_store.commit_task_completions((TaskCompletion(
            task_id=task_id, status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="",
            answer_ref=answer_ref,
            result=normalize_execution_result(passes=(ExecutionPassResult(
                number=1, capture_status=CAPTURE_COMPLETE, runresult="correct", verdict="OK",
                score_text="", answer_correct=True, usage=ExecutionUsage(), feedback="",
                artifacts=PassArtifacts(
                    input_ref=input_ref, output_ref=answer_ref,
                    stderr_ref=empty, system_ref=empty, judge_message_ref=empty, team_message_ref=empty,
                    metadata_ref=empty, compare_metadata_ref=empty,
                ),
            ),), verdict="OK", answer_correct=True),
        ),))
        logs = runtime.storage_layout.prepare_verification_layout(verification_id).logs
        observed: list[tuple[str, bytes, bytes]] = []

        def reply(work: DomjudgeWork) -> JudgehostReply:
            name, source_input, answer, result = self._execute(work)
            observed.append((name, source_input, answer))
            return result

        with reporting_judgehost(runtime.judgehost_task_service, reply):
            result = runtime.verification_sanity_service.validate_sample_outputs(
                problem=self.problem, user=self.user, verification_id=verification_id,
                logs_dir=logs, test_plans=[self._plan("001.in", custom=False)],
            )
        self.assertEqual(result.status, "passed", result.error)
        self.assertEqual(observed, [("custom_sample_output.py", b"base\n", b"ok\n")])
        self.assertEqual((logs / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n")

    def test_custom_inputs_share_accepted_execution_and_report_output_mismatch(self) -> None:
        main_source = runtime.runtime_blob_store.put_bytes(
            b"import sys\nsys.stdout.buffer.write(b'answer-' + sys.stdin.buffer.read())\n"
        )
        payload_base = {
            "problem_mode": "pass-fail", "run_config_json": "{}",
            "problem_limits": {"time_limit_ms": 2000, "memory_limit_mb": 1024, "pass_limit": 1},
            "source_files": {},
        }
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                verification_id = canonical_test_verification_id(self.random_id("custom-samples"))
                logs = runtime.storage_layout.prepare_verification_layout(verification_id).logs
                plans = [self._plan(test, custom=True) for test in ("001.in", "002.in")]
                if mismatch:
                    plans[1] = replace(plans[1], sample_output_text="incorrect\n")
                main_jobs: set[int] = set()
                inputs: list[bytes] = []
                validated_answers: list[bytes] = []

                def reply(work: DomjudgeWork) -> JudgehostReply:
                    name, source_input, answer, result = self._execute(work)
                    if name == "std.py":
                        main_jobs.add(work["jobid"])
                        inputs.append(source_input)
                    else:
                        validated_answers.append(answer)
                    return result

                with reporting_judgehost(runtime.judgehost_task_service, reply):
                    result = runtime.verification_sanity_service.validate_sample_outputs(
                        problem=self.problem, user=self.user, verification_id=verification_id,
                        logs_dir=logs, test_plans=plans,
                        accepted_source_label="solutions/std.py", accepted_source_name="std.py",
                        accepted_source_file=main_source, run_verification_payload_base=payload_base,
                        bypass_case_result_cache=True,
                    )
                self.assertEqual(len(main_jobs), 1)
                self.assertEqual(inputs, [b"custom-001.in\n", b"custom-002.in\n"])
                self.assertEqual(validated_answers, [b"answer-custom-001.in\n", b"answer-custom-002.in\n"])
                if mismatch:
                    self.assertEqual((result.status, result.validated_count, result.failed_test), ("failed", 1, "002.in"))
                    self.assertIn("002.in", result.error)
                    self.assertIn("002.in: failed", (logs / "validate.log").read_text(encoding="utf-8"))
                else:
                    self.assertEqual((result.status, result.validated_count), ("passed", 2), result.error)
                    self.assertEqual((logs / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n002.in: ok\n")
