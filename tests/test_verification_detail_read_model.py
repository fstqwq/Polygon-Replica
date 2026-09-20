import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from app.service.execution.policy import normalize_execution_result
from app.service.execution.codec import execution_result_json
from app.service.verification.detail_read_model import VerificationTestDetailReadModel
from app.service.verification.lifecycle import ActivationPlan, PlannedTask, VerificationSnapshotRecord, VerificationTaskKind, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_execute
from tests.verification_service_fixture import VerificationServiceTestBase, make_execution_result, multi_pass_result


class TestVerificationDetailReadModel(VerificationServiceTestBase):
    def _fixture(self, suffix: str = "") -> tuple[str, tuple[PlannedTask, ...]]:
        verification_id = canonical_test_verification_id(self.test_id + suffix)
        self._insert_verification_row(verification_id)
        kinds: dict[str, VerificationTaskKind] = {"generator-0": "generate-input", "accepted": "main-correct", "solution-0": "solution-run", "solution-1": "solution-run"}
        tasks = tuple(
            PlannedTask(
                task_id=verification_task_id(verification_id, program, test),
                predecessor_task_id=None, task_kind=kind,
                source_path=f"solutions/{program}.cpp", program_id=program,
                test_name=test, expected_behavior="accepted",
            ) for test in ("001.in", "002.in", "003.in") for program, kind in kinds.items()
        )
        self.verification_service.activate_verification(ActivationPlan.build(
            verification_id, detail={"mode": "pass-fail", "pass_limit": 2}, tasks=tasks,
            programs=tuple(self._verification_program(
                program_id=program, kind=kind, source_path=f"solutions/{program}.cpp", expected_behavior="accepted",
            ) for program, kind in kinds.items()),
        ))
        return verification_id, tasks

    def _read(
        self,
        verification_id: str,
        test: str = "002.in",
        program: str | None = "solution-0",
        authorize: Callable[[VerificationSnapshotRecord], None] | None = None,
    ) -> VerificationTestDetailReadModel:
        result = self.verification_service.verification_test_detail_read_model(
            verification_id, test_name=test, program_id=program,
            authorize=authorize or (lambda _record: None),
        )
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_scoped_tasks_all_programs_and_complete_pass_evidence(self):
        verification_id, tasks = self._fixture()
        task = next(task for task in tasks if task.program_id == "solution-0" and task.test_name == "002.in")
        result = multi_pass_result("blob://final")
        self.verification_task_store.commit_task_completions((TaskCompletion(
            task_id=task.task_id, status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="", result=result,
        ),))
        scoped = self._read(verification_id)
        self.assertEqual({row["program_id"] for row in scoped["tasks"]}, {"generator-0", "accepted", "solution-0"})
        self.assertEqual({row["test_name"] for row in scoped["tasks"]}, {"002.in"})
        self.assertEqual(len(scoped["cases"]), 1)
        self.assertEqual(scoped["cases"][0]["result"].passes, result.passes)
        self.assertEqual([item.number for item in scoped["cases"][0]["result"].passes], [1, 2])
        all_programs = self._read(verification_id, program=None)
        self.assertEqual({row["program_id"] for row in all_programs["tasks"]}, {"generator-0", "accepted", "solution-0", "solution-1"})
        full = self.verification_service.verification_detail_read_model(verification_id)
        self.assertEqual(len(full["tasks"]), 12)
        self.assertEqual(full["task_counts"]["total"], 12)

    def test_authorization_precedes_detail_and_evidence_materialization(self) -> None:
        for section, sql in (
            ("detail", "UPDATE verifications SET pass_limit='invalid' WHERE id=?"),
            ("evidence", "UPDATE verification_tasks SET result_json='invalid' WHERE verification_id=?"),
        ):
            with self.subTest(section=section):
                verification_id, _tasks = self._fixture(section)
                isolated_db_execute(self.db, sql, [verification_id])

                def denied(record: VerificationSnapshotRecord) -> None:
                    self.assertEqual(record["id"], verification_id)
                    raise PermissionError("denied")

                # A permitted reader reaches the corrupt persisted data. An
                # unauthorized reader must be rejected before decoding it.
                with self.assertRaises(ValueError):
                    self._read(verification_id)
                with self.assertRaisesRegex(PermissionError, "denied"):
                    self._read(verification_id, authorize=denied)

    def test_cancellation_commit_keeps_current_snapshot_and_next_request_sees_it(self):
        verification_id, _tasks = self._fixture()
        entered = threading.Event()
        resume = threading.Event()
        def authorize(_record):
            entered.set()
            if not resume.wait(10):
                raise TimeoutError("snapshot barrier")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._read, verification_id, authorize=authorize)
            try:
                self.assertTrue(entered.wait(10))
                self.verification_service.cancel_verification(verification_id, reason="cancel while reading")
            finally:
                resume.set()
            before = pending.result(timeout=10)
        self.assertEqual(before["record"]["status"], "running")
        self.assertEqual({row["status"] for row in before["tasks"]}, {VerificationTaskStatus.PENDING})
        after = self._read(verification_id)
        self.assertEqual(after["record"]["status"], "cancelled")
        self.assertEqual({row["status"] for row in after["tasks"]}, {VerificationTaskStatus.CANCELLED})

    def test_duplicate_owner_and_artifact_first_owner_use_distinct_orderings(self):
        for predecessor in (False, True):
            with self.subTest(predecessor=predecessor):
                self.test_id += str(predecessor)
                verification_id, tasks = self._fixture()
                generators = {task.test_name: task for task in tasks if task.program_id == "generator-0"}
                output = "blob://shared-output"
                result = make_execution_result(verdict="OK", output_ref=output)
                # Completion order determines duplicate ownership; task-id order
                # independently chooses the input/answer artifact for this test.
                for test in ("003.in", "001.in"):
                    self.verification_task_store.commit_task_completions((TaskCompletion(
                        task_id=generators[test].task_id, status=VerificationTaskStatus.DONE,
                        run_id="", judgehost_task_id="", result=result,
                    ),))
                duplicate = generators["002.in"]
                if predecessor:
                    isolated_db_execute(self.db, "UPDATE verification_tasks SET predecessor_task_id=? WHERE id=?", [generators["001.in"].task_id, duplicate.task_id])
                skipped = replace(result, outcome=replace(result.outcome, verdict="SK"))
                self.verification_task_store.commit_task_completions((TaskCompletion(
                    task_id=duplicate.task_id, status=VerificationTaskStatus.DONE,
                    run_id="", judgehost_task_id="", result=skipped,
                ),))
                for test, finished in (("003.in", "2026-01-01"), ("001.in", "2026-01-02")):
                    isolated_db_execute(self.db, "UPDATE verification_tasks SET finished_at=?, result_json=? WHERE id=?", [finished, execution_result_json(result), generators[test].task_id])
                owners = sorted(task.task_id for task in tasks if task.test_name == "002.in" and task.program_id in {"solution-0", "solution-1"})
                for index, task_id in enumerate(reversed(owners)):
                    self.verification_task_store.commit_task_completions((TaskCompletion(
                        task_id=task_id, status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="",
                        result=normalize_execution_result(verdict="OK"), input_ref=f"blob://input-{index}", answer_ref=f"blob://answer-{index}",
                    ),))
                scoped = self._read(verification_id)
                expected_owner = "001.in" if predecessor else "003.in"
                self.assertEqual({row["test_name"] for row in scoped["tasks"]}, {"002.in", expected_owner})
                selected = next(row for row in scoped["tasks"] if row["id"] == duplicate.task_id)
                self.assertEqual(selected["input_ref"], "blob://input-1")
                self.assertEqual(selected["answer_ref"], "blob://answer-1")
