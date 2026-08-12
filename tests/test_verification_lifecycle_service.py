from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable

from app.service.verification.execution import (
    VerificationCoordinatorFailure,
    VerificationExecutionCallbacks,
    VerificationExecutionService,
)
from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    SanityFinish,
    verification_task_id,
)
from app.service.verification.task_completion import CompletionCommit, TaskCompletion
from app.service.verification.task_scheduler import TaskPublishResult
from app.service.verification.runtime_registry import (
    VerificationRuntimeHandle,
    VerificationRuntimeRegistry,
)
from app.service.verification.types import VerificationTaskStatus

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_all
from tests.verification_service_fixture import (
    VerificationServiceTestBase,
    make_execution_result,
    terminal_report,
)


class _RecordingDrainer:
    def __init__(self, before_record: Callable[[], None] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._before_record = before_record

    def request_verification_cancel(
        self,
        verification_id: str,
        reason: str,
    ) -> dict[str, int]:
        if self._before_record is not None:
            self._before_record()
        self.calls.append((verification_id, reason))
        return {
            "cancelled_cases": 1,
            "awaiting_receipts": 2,
            "affected_tasks": 3,
            "affected_batches": 4,
        }


class _CancellationHandle(VerificationRuntimeHandle):
    def __init__(self, on_cancel: Callable[[str], None]) -> None:
        self._on_cancel = on_cancel
        self.closed = False

    def enqueue_case_leased(self, verification_task_id: str) -> None:
        del verification_task_id

    def enqueue_completion_committed(self, commit: CompletionCommit) -> None:
        del commit

    def enqueue_cancel(self, reason: str) -> None:
        self._on_cancel(reason)

    def enqueue_closed(self) -> None:
        self.closed = True


class TestVerificationLifecycleService(VerificationServiceTestBase):
    def test_activation_installs_one_immutable_graph(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-once:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                self._verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        first = self.verification_service.activate_verification(plan)
        duplicate = self.verification_service.activate_verification(plan)

        self.assertEqual(first.outcome, "activated")
        self.assertEqual(duplicate.outcome, "already-running")
        rows = self.verification_task_store.list_rows(verification_id)
        self.assertEqual([str(row["id"]) for row in rows], [task_id])

    def test_activation_rejects_task_identity_mismatch_before_writing(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-identity:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        wrong_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                self._verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=wrong_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="002.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not match its plan identity"):
            self.verification_service.activate_verification(plan)

        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(self.verification_task_store.list_rows(verification_id), [])

    def test_activation_rejects_inconsistent_program_membership(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-program:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        accepted_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        solution_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                self._verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
                self._verification_program(
                    program_id="solution-0",
                    kind="solution-run",
                    source_path="solutions/a.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=accepted_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
                PlannedTask(
                    task_id=solution_id,
                    predecessor_task_id=accepted_id,
                    task_kind="solution-run",
                    source_path="solutions/b.cpp",
                    program_id="solution-0",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not match its program"):
            self.verification_service.activate_verification(plan)

        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(self.verification_task_store.list_rows(verification_id), [])

    def test_activation_rolls_back_parent_detail_and_graph(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-rollback:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={
                "mode": "interactive",
                "selected_test_names": ["001.in"],
            },
            programs=(
                self._verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )
        self._install_activation_abort()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced activation task failure",
            ):
                self.verification_service.activate_verification(plan)
        finally:
            self._clear_activation_abort()

        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "queued")
        self.assertEqual(self.verification_task_store.list_rows(verification_id), [])
        self.assertEqual(
            self.verification_service.verification_detail(verification_id)[
                "selected_test_names"
            ],
            [],
        )

    def test_activation_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"activation-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        plan = ActivationPlan.build(
            verification_id,
            detail={"mode": "pass-fail"},
            programs=(
                self._verification_program(
                    program_id="accepted",
                    kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    expected_behavior="accepted",
                ),
            ),
            tasks=(
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                ),
            ),
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _activate() -> None:
            try:
                barrier.wait()
                outcomes["activate"] = (
                    self.verification_service.activate_verification(plan).outcome
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = self.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_activate),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(outcomes["cancel"], "transitioned")
        self.assertIn(outcomes["activate"], {"activated", "closed"})
        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "cancelled")
        task_rows = self.verification_task_store.list_rows(verification_id)
        if outcomes["activate"] == "activated":
            self.assertEqual(len(task_rows), 1)
            self.assertEqual(
                str(task_rows[0]["status"]),
                VerificationTaskStatus.CANCELLED,
            )
        else:
            self.assertEqual(task_rows, [])

    def test_completion_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                }
            ],
            edges=[],
            detail={"mode": "pass-fail", "sanity_status": ""},
        )
        completion = TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStatus.DONE,
            run_id="run-completion-cancel",
            judgehost_task_id="judgehost-completion-cancel",
            result=normalize_execution_result(verdict="OK"),
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _complete() -> None:
            try:
                barrier.wait()
                commit = self.verification_task_completion_service.commit(
                    (completion,)
                )
                outcomes["completion"] = (
                    "committed"
                    if task_id in commit.committed_task_ids
                    else "already-terminal"
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = self.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_complete),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        task = self.verification_task_store.list_rows(verification_id)[0]
        if str(parent["status"]) == "ok":
            self.assertEqual(outcomes, {
                "completion": "committed",
                "cancel": "closed",
            })
            self.assertEqual(task["status"], VerificationTaskStatus.DONE)
        else:
            self.assertEqual(str(parent["status"]), "cancelled")
            self.assertEqual(outcomes, {
                "completion": "already-terminal",
                "cancel": "transitioned",
            })
            self.assertEqual(
                task["status"],
                VerificationTaskStatus.CANCELLED,
            )
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStatus.DONE,
                    VerificationTaskStatus.FAILED,
                    VerificationTaskStatus.CANCELLED,
                }
                for row in self.verification_task_store.list_rows(
                    verification_id
                )
            )
        )

    def test_finish_sanity_and_cancel_have_one_serial_outcome(self) -> None:
        verification_id = canonical_test_verification_id(
            f"sanity-cancel-race:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                }
            ],
            edges=[],
            detail={"mode": "pass-fail", "sanity_status": "pending"},
        )
        completion = self.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="run-sanity-cancel",
                    judgehost_task_id="judgehost-sanity-cancel",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )
        self.assertTrue(completion.sanity_claimed)
        self.assertEqual(completion.parent_transition, "sanity-running")
        finish = SanityFinish.build(
            verification_id,
            detail={
                "mode": "pass-fail",
                "sanity_status": "passed",
                "sanity_checked_count": 1,
            },
        )
        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}
        failures: list[BaseException] = []

        def _finish() -> None:
            try:
                barrier.wait()
                outcomes["finish"] = self.verification_service.finish_sanity(
                    finish
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        def _cancel() -> None:
            try:
                barrier.wait()
                outcomes["cancel"] = self.verification_service.cancel_verification(
                    verification_id,
                    reason="verification cancelled by user",
                ).outcome
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = (
            threading.Thread(target=_finish),
            threading.Thread(target=_cancel),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(failures, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        if str(parent["status"]) == "ok":
            self.assertEqual(outcomes, {
                "finish": "transitioned",
                "cancel": "closed",
            })
            self.assertEqual(str(parent["sanity_status"]), "passed")
        else:
            self.assertEqual(str(parent["status"]), "cancelled")
            self.assertEqual(outcomes, {
                "finish": "closed",
                "cancel": "transitioned",
            })
            self.assertEqual(str(parent["sanity_status"]), "skipped")
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStatus.DONE,
                    VerificationTaskStatus.FAILED,
                    VerificationTaskStatus.CANCELLED,
                }
                for row in self.verification_task_store.list_rows(
                    verification_id
                )
            )
        )

    def test_verification_lifecycle_rows_satisfy_aggregate_invariants(self) -> None:
        queued_id = canonical_test_verification_id(
            f"invariant-queued:{self.test_id}"
        )
        running_id = canonical_test_verification_id(
            f"invariant-running:{self.test_id}"
        )
        ok_id = canonical_test_verification_id(f"invariant-ok:{self.test_id}")
        cancelled_id = canonical_test_verification_id(
            f"invariant-cancelled:{self.test_id}"
        )
        for verification_id in (queued_id, running_id, ok_id, cancelled_id):
            self._insert_verification_row(verification_id)

        def _accepted_task(verification_id: str) -> dict[str, object]:
            return {
                "id": verification_task_id(
                    verification_id,
                    "accepted",
                    "001.in",
                ),
                "task_kind": "main-correct",
                "source_path": "solutions/accepted.cpp",
                "program_id": "accepted",
                "test_name": "001.in",
                "expected_behavior": "accepted",
            }

        running_task = _accepted_task(running_id)
        ok_task = _accepted_task(ok_id)
        self._activate_graph(
            running_id,
            tasks=[running_task],
            edges=[],
        )
        self._activate_graph(ok_id, tasks=[ok_task], edges=[])
        self.verification_task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=str(ok_task["id"]),
                    status=VerificationTaskStatus.DONE,
                    run_id="run-invariant-ok",
                    judgehost_task_id="judgehost-invariant-ok",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )
        cancelled = self.verification_service.cancel_verification(
            cancelled_id,
            reason="invariant terminal fixture",
        )
        self.assertEqual(cancelled.outcome, "transitioned")

        violations = isolated_db_fetch_all(
            self.db,
            """
            SELECT id,'queued-has-tasks' AS violation
            FROM verifications verification
            WHERE status='queued'
              AND EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'running-without-graph'
            FROM verifications verification
            WHERE status='running'
              AND NOT EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'terminal-has-open-task'
            FROM verifications verification
            WHERE status IN ('ok','failed','cancelled')
              AND EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
                    AND task.final_status=''
              )
            UNION ALL
            SELECT id,'ok-without-graph'
            FROM verifications verification
            WHERE status='ok'
              AND NOT EXISTS (
                  SELECT 1 FROM verification_tasks task
                  WHERE task.verification_id=verification.id
              )
            UNION ALL
            SELECT id,'ok-with-active-sanity'
            FROM verifications
            WHERE status='ok' AND sanity_status IN ('pending','running')
            """
        )
        self.assertEqual(
            [(str(row["id"]), str(row["violation"])) for row in violations],
            [],
        )

    def _commit_solution_result(
        self,
        verification_id: str,
        task_id: str,
        verdict: str,
    ) -> tuple[TaskCompletion, CompletionCommit]:
        task_row = self.verification_task_store.runtime_row(task_id)
        assert task_row is not None
        completion = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id=task_row["judgehost_task_id"],
                verification_id=verification_id,
                run_id=task_row["run_id"],
                result=make_execution_result(verdict=verdict),
                summary={"tests": [{"verdict": verdict}]},
            ),
        )
        commit = self.verification_task_completion_service.commit(
            (completion,),
            notify=False,
        )
        return completion, commit

    def test_program_required_verdict_uses_all_testcases(self) -> None:
        verification_id = canonical_test_verification_id(
            f"program-required:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        first_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        second_task_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": first_task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/rejected.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "rejected",
                    "status": VerificationTaskStatus.QUEUED,
                },
                {
                    "id": second_task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/rejected.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "rejected",
                    "status": VerificationTaskStatus.QUEUED,
                },
            ],
            edges=[],
        )

        first_completion, first_commit = self._commit_solution_result(
            verification_id,
            first_task_id,
            "WA",
        )
        self.assertEqual(first_completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(first_completion.fail_reason, "")
        self.assertEqual(first_commit.parent_transition, "")
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "running")
        self.assertEqual(str(parent["fail_reason"]), "")

        second_completion, second_commit = self._commit_solution_result(
            verification_id,
            second_task_id,
            "AC",
        )
        self.assertEqual(second_completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(second_completion.fail_reason, "")
        self.assertEqual(second_commit.parent_transition, "ok")
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "ok")
        self.assertEqual(str(parent["fail_reason"]), "")

    def test_missing_program_required_verdict_fails_after_last_testcase(self) -> None:
        verification_id = canonical_test_verification_id(
            f"program-required-missing:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_ids = tuple(
            verification_task_id(verification_id, "solution-0", test_name)
            for test_name in ("001.in", "002.in")
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/rejected.cpp",
                    "program_id": "solution-0",
                    "test_name": test_name,
                    "expected_behavior": "rejected",
                    "status": VerificationTaskStatus.QUEUED,
                }
                for task_id, test_name in zip(
                    task_ids,
                    ("001.in", "002.in"),
                    strict=True,
                )
            ],
            edges=[],
        )

        first_completion, first_commit = self._commit_solution_result(
            verification_id,
            task_ids[0],
            "AC",
        )
        self.assertEqual(first_completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(first_commit.parent_transition, "")
        self.assertEqual(first_commit.failure_reason, "")

        second_completion, second_commit = self._commit_solution_result(
            verification_id,
            task_ids[1],
            "AC",
        )
        self.assertEqual(second_completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(second_completion.fail_reason, "")
        self.assertEqual(second_commit.parent_transition, "failed")
        self.assertIn(
            "required=[WA, TL, RE, CE]",
            second_commit.failure_reason,
        )
        self.assertIn("got=[AC]", second_commit.failure_reason)
        rows = {
            str(row["id"]): row
            for row in self.verification_task_store.list_rows(verification_id)
        }
        for task_id in task_ids:
            self.assertEqual(
                str(rows[task_id]["status"]),
                VerificationTaskStatus.DONE,
            )

    def test_solution_mismatch_waits_for_graph_then_fails_parent(self) -> None:
        verification_id = canonical_test_verification_id(
            f"solution-mismatch:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        accepted_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        rejected_id = verification_task_id(
            verification_id,
            "solution-1",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": accepted_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/expected-ac.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                },
                {
                    "id": rejected_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/expected-wa.cpp",
                    "program_id": "solution-1",
                    "test_name": "001.in",
                    "expected_behavior": "wrong_answer",
                },
            ],
            edges=[],
        )
        task_store = self.verification_task_store
        for task_id, run_id, judgehost_task_id in (
            (accepted_id, "r-expected-ac", "jt-expected-ac"),
            (rejected_id, "r-expected-wa", "jt-expected-wa"),
        ):
            self.assertTrue(
                task_store.bind_and_expose_judgehost_runtime(
                    task_id,
                    run_id=run_id,
                    judgehost_task_id=judgehost_task_id,
                    expose=lambda: None,
                )
            )
        wa_summary = {"tests": [{"verdict": "WA"}]}
        mismatch_row = task_store.runtime_row(accepted_id)
        assert mismatch_row is not None
        mismatch = self.verification_task_completion_service.prepare(
            mismatch_row,
            terminal_report(
                judgehost_task_id="jt-expected-ac",
                verification_id=verification_id,
                run_id="r-expected-ac",
                result=make_execution_result(verdict="WA"),
                summary=wa_summary,
            ),
        )
        first_commit = self.verification_task_completion_service.commit(
            (mismatch,),
            notify=False,
        )
        self.assertEqual(mismatch.status, VerificationTaskStatus.FAILED)
        self.assertIn("allowed=[AC]", mismatch.fail_reason)
        self.assertEqual(first_commit.parent_transition, "")
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "running")
        self.assertEqual(str(parent["fail_reason"]), mismatch.fail_reason)

        matched_row = task_store.runtime_row(rejected_id)
        assert matched_row is not None
        matched = self.verification_task_completion_service.prepare(
            matched_row,
            terminal_report(
                judgehost_task_id="jt-expected-wa",
                verification_id=verification_id,
                run_id="r-expected-wa",
                result=make_execution_result(verdict="WA"),
                summary=wa_summary,
            ),
        )
        final_commit = self.verification_task_completion_service.commit(
            (matched,),
            notify=False,
        )
        self.assertEqual(matched.status, VerificationTaskStatus.DONE)
        self.assertEqual(matched.fail_reason, "")
        self.assertEqual(final_commit.parent_transition, "failed")
        parent = self.verification_service.verification_record(verification_id)
        assert parent is not None
        self.assertEqual(str(parent["status"]), "failed")
        self.assertEqual(str(parent["fail_reason"]), mismatch.fail_reason)



    def test_cancel_terminalizes_leased_and_pending_tasks(self) -> None:
        verification_id = canonical_test_verification_id("cancel")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        running_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        pending_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": running_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStatus.PENDING,
                },
            ],
            edges=[],
        )
        transition = self.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(transition.outcome, "transitioned")
        retry = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=running_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-a",
                    judgehost_task_id="jt-a",
                    result=make_execution_result(verdict="AC"),
                ),
            )
        )
        rows = {
            str(row["id"]): row
            for row in task_store.list_rows(verification_id)
        }
        self.assertEqual(retry.already_terminal_task_ids, frozenset({running_id}))
        self.assertEqual(
            str(rows[running_id]["status"]),
            VerificationTaskStatus.CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStatus.CANCELLED,
        )

    def test_cancel_persists_reason_and_terminalizes_task(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-cancel-reason:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                }
            ],
            edges=[],
        )
        transition = self.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(transition.outcome, "transitioned")
        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "cancelled")
        self.assertEqual(
            str(row["fail_reason"] or ""),
            "verification cancelled by user",
        )
        task_row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(
            str(task_row["status"]),
            VerificationTaskStatus.CANCELLED,
        )

    def test_startup_recovery_terminalizes_running_graph(self) -> None:
        verification_id = canonical_test_verification_id("startup-reconcile")
        self._insert_verification_row(verification_id)
        running_id = verification_task_id(
            verification_id,
            "solution-0",
            "001.in",
        )
        pending_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        task_store = self.verification_task_store
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": running_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.LEASED,
                    "started_at": "2026-03-23T00:00:00Z",
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStatus.PENDING,
                },
            ],
            edges=[],
            detail={"mode": "pass-fail"},
        )

        summary = self.verification_service.recover_startup(
            reason="interrupted by application restart"
        )

        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(summary.verification_ids, (verification_id,))
        self.assertEqual(
            str(rows[running_id]["status"]),
            VerificationTaskStatus.CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStatus.CANCELLED,
        )
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")

    def test_startup_recovery_fails_queued_verification_without_graph(self) -> None:
        verification_id = canonical_test_verification_id("startup-queued")
        self._insert_verification_row(verification_id)

        summary = self.verification_service.recover_startup(
            reason="interrupted by application restart"
        )

        self.assertEqual(summary.verification_ids, (verification_id,))
        self.assertEqual(summary.cancelled_task_ids, ())
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"] or ""), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"] or ""),
            "interrupted by application restart",
        )
        self.assertTrue(str(verification_row["finished_at"] or ""))

    def test_failure_transition_preserves_first_reason(self) -> None:
        verification_id = canonical_test_verification_id(
            f"task-store:{self.test_id}"
        )
        self._insert_verification_row(verification_id)
        first = self.verification_service.fail_verification(
            verification_id,
            reason=(
                "generate-input / generators/gen.cpp / 001.in: "
                "validator failed"
            ),
        )
        second = self.verification_service.cancel_verification(
            verification_id,
            reason="verification cancelled by user",
        )
        self.assertEqual(first.outcome, "transitioned")
        self.assertEqual(second.outcome, "closed")
        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "failed")
        self.assertEqual(
            str(row["fail_reason"]),
            "generate-input / generators/gen.cpp / 001.in: validator failed",
        )

    def test_cancelled_transition_wins_over_later_failure(self) -> None:
        verification_id = canonical_test_verification_id(
            f"cancel-first:{self.test_id}"
        )
        self._insert_verification_row(verification_id)

        first = self.verification_service.cancel_verification(
            verification_id,
            reason="infrastructure failure words do not change this status",
        )
        second = self.verification_service.fail_verification(
            verification_id,
            reason="late scheduler failure",
        )

        self.assertEqual(first.outcome, "transitioned")
        self.assertEqual(second.outcome, "closed")
        row = self.verification_service.verification_record(verification_id)
        assert row is not None
        self.assertEqual(str(row["status"]), "cancelled")
        self.assertEqual(
            str(row["fail_reason"]),
            "infrastructure failure words do not change this status",
        )

    def test_execution_cancel_persists_before_event_and_drain(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-cancel:{self.test_id}"
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        registry = VerificationRuntimeRegistry()
        event_order: list[str] = []

        def _assert_terminal(stage: str) -> None:
            snapshot = self.verification_service.verification_snapshot(
                verification_id
            )
            assert snapshot is not None
            self.assertEqual(snapshot["record"]["status"], "cancelled")
            rows = {str(row["id"]): row for row in snapshot["tasks"]}
            self.assertEqual(
                str(rows[task_id]["status"]),
                VerificationTaskStatus.CANCELLED,
            )
            event_order.append(stage)

        handle = _CancellationHandle(lambda _reason: _assert_terminal("event"))
        registry.register(verification_id, handle)
        drainer = _RecordingDrainer(lambda: _assert_terminal("drain"))
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            registry,
            drainer,
        )

        result = execution_service.cancel_verification(
            verification_id,
            reason="cancelled in test",
        )

        self.assertEqual(result.transition.outcome, "transitioned")
        self.assertEqual(event_order, ["event", "drain"])
        self.assertEqual(
            drainer.calls,
            [(verification_id, "cancelled in test")],
        )
        self.assertEqual(result.drain["awaiting_receipts"], 2)
        self.assertTrue(registry.unregister(verification_id, handle))

    def test_execution_observes_cancellation_before_runtime_registration(
        self,
    ) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-cancel-before-register:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        registry = VerificationRuntimeRegistry()
        drainer = _RecordingDrainer()
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            registry,
            drainer,
        )

        cancellation = execution_service.cancel_verification(
            verification_id,
            reason="cancelled before registration",
        )
        published: list[str] = []
        execution_service.run(
            verification_id,
            callbacks=VerificationExecutionCallbacks(
                publish_task=lambda row: (
                    published.append(str(row["id"]))
                    or TaskPublishResult(str(row["id"]), "run", "judgehost")
                ),
                probe_task_case_cache=lambda _task_ids: set(),
                close_programs=lambda _program_ids: None,
            ),
            edges=[],
        )

        self.assertEqual(cancellation.transition.outcome, "transitioned")
        self.assertEqual(published, [])
        self.assertEqual(
            drainer.calls,
            [(verification_id, "cancelled before registration")],
        )

    def test_execution_does_not_drain_when_sqlite_transition_fails(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-cancel-sqlite-failure:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        drainer = _RecordingDrainer()
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            VerificationRuntimeRegistry(),
            drainer,
        )

        self._install_verification_cancel_abort(verification_id)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced cancellation failure",
            ):
                execution_service.cancel_verification(
                    verification_id,
                    reason="cancelled in test",
                )
        finally:
            self._clear_verification_cancel_abort()

        self.assertEqual(drainer.calls, [])
        record = self.verification_service.verification_record(verification_id)
        assert record is not None
        self.assertEqual(str(record["status"]), "running")
        task_rows = self.verification_task_store.list_rows(verification_id)
        self.assertEqual(
            [str(row["status"]) for row in task_rows],
            [VerificationTaskStatus.PENDING],
        )

    def test_execution_cancel_falls_back_to_closed_event_and_drains(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-cancel-event-failure:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )

        class FailingHandle(_CancellationHandle):
            def enqueue_cancel(self, reason: str) -> None:
                del reason
                raise RuntimeError("runtime event queue unavailable")

        registry = VerificationRuntimeRegistry()
        handle = FailingHandle(lambda _reason: None)
        registry.register(verification_id, handle)
        drainer = _RecordingDrainer()
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            registry,
            drainer,
        )

        result = execution_service.cancel_verification(
            verification_id,
            reason="cancelled in test",
        )

        self.assertEqual(result.transition.outcome, "transitioned")
        self.assertTrue(handle.closed)
        self.assertEqual(
            drainer.calls,
            [(verification_id, "cancelled in test")],
        )
        self.assertTrue(registry.unregister(verification_id, handle))

    def test_execution_cancel_stops_real_coordinator_when_cancel_event_fails(
        self,
    ) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-real-event-failure:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )

        class FailingCancelRegistry(VerificationRuntimeRegistry):
            def cancelled(
                self,
                runtime_verification_id: str,
                reason: str,
            ) -> bool:
                del runtime_verification_id, reason
                raise RuntimeError("runtime cancel event unavailable")

        registry = FailingCancelRegistry()
        drainer = _RecordingDrainer()
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            registry,
            drainer,
        )
        published = threading.Event()
        run_errors: list[Exception] = []

        def _publish(row: dict[str, object]) -> TaskPublishResult:
            published.set()
            task_id = str(row["id"])
            return TaskPublishResult(
                task_id,
                f"run-{task_id}",
                f"judgehost-{task_id}",
            )

        def _run() -> None:
            try:
                execution_service.run(
                    verification_id,
                    callbacks=VerificationExecutionCallbacks(
                        publish_task=_publish,
                        probe_task_case_cache=lambda _task_ids: set(),
                        close_programs=lambda _program_ids: None,
                    ),
                    edges=[],
                )
            except Exception as exc:  # surfaced below in the test thread
                run_errors.append(exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        self.assertTrue(published.wait(timeout=2.0))

        result = execution_service.cancel_verification(
            verification_id,
            reason="cancelled in test",
        )
        thread.join(timeout=2.0)

        self.assertEqual(result.transition.outcome, "transitioned")
        self.assertFalse(thread.is_alive())
        self.assertEqual(run_errors, [])
        self.assertEqual(
            drainer.calls,
            [(verification_id, "cancelled in test")],
        )

    def test_execution_cancel_retries_drain_after_closed_transition(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-closed-drain-retry:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        drain_calls: list[tuple[str, str]] = []

        class RetryDrainer:
            def request_verification_cancel(
                self,
                runtime_verification_id: str,
                reason: str,
            ) -> dict[str, int]:
                drain_calls.append((runtime_verification_id, reason))
                if len(drain_calls) <= 2:
                    raise RuntimeError("Judgehost drain unavailable")
                return {
                    "cancelled_cases": 1,
                    "awaiting_receipts": 0,
                    "affected_tasks": 1,
                    "affected_batches": 1,
                }

        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            VerificationRuntimeRegistry(),
            RetryDrainer(),
        )

        with self.assertRaisesRegex(RuntimeError, "drain unavailable"):
            execution_service.cancel_verification(
                verification_id,
                reason="cancelled in test",
            )
        retry = execution_service.cancel_verification(
            verification_id,
            reason="cancelled in test",
        )

        self.assertEqual(retry.transition.outcome, "closed")
        self.assertEqual(retry.drain["cancelled_cases"], 1)
        self.assertEqual(
            drain_calls,
            [
                (verification_id, "cancelled in test"),
                (verification_id, "cancelled in test"),
                (verification_id, "cancelled in test"),
            ],
        )

    def test_execution_reconciles_cancellation_during_registration(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-register-cancel:{self.test_id}"
        )
        self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )

        lifecycle = self.verification_service

        class CancellingRegistry(VerificationRuntimeRegistry):
            def register(
                self,
                runtime_verification_id: str,
                handle: VerificationRuntimeHandle,
            ) -> None:
                super().register(runtime_verification_id, handle)
                lifecycle.cancel_verification(
                    runtime_verification_id,
                    reason="cancelled during registration",
                )

        published: list[str] = []
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            CancellingRegistry(),
            _RecordingDrainer(),
        )
        execution_service.run(
            verification_id,
            callbacks=VerificationExecutionCallbacks(
                publish_task=lambda row: (
                    published.append(str(row["id"]))
                    or TaskPublishResult(str(row["id"]), "run", "judgehost")
                ),
                probe_task_case_cache=lambda _task_ids: set(),
                close_programs=lambda _program_ids: None,
            ),
            edges=[],
        )

        self.assertEqual(published, [])
        snapshot = self.verification_service.verification_snapshot(
            verification_id
        )
        assert snapshot is not None
        self.assertEqual(snapshot["record"]["status"], "cancelled")

    def test_scheduler_failure_persists_before_judgehost_drain(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-scheduler-failure:{self.test_id}"
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )

        def _assert_failed_before_drain() -> None:
            snapshot = self.verification_service.verification_snapshot(
                verification_id
            )
            assert snapshot is not None
            self.assertEqual(snapshot["record"]["status"], "failed")
            rows = {str(row["id"]): row for row in snapshot["tasks"]}
            self.assertEqual(
                str(rows[task_id]["status"]),
                VerificationTaskStatus.CANCELLED,
            )

        drainer = _RecordingDrainer(_assert_failed_before_drain)
        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            VerificationRuntimeRegistry(),
            drainer,
        )

        def _fail_publish(_row: dict[str, object]) -> TaskPublishResult:
            raise RuntimeError("publisher failed")

        with self.assertRaisesRegex(
            VerificationCoordinatorFailure,
            "publisher failed",
        ):
            execution_service.run(
                verification_id,
                callbacks=VerificationExecutionCallbacks(
                    publish_task=_fail_publish,
                    probe_task_case_cache=lambda _task_ids: set(),
                    close_programs=lambda _program_ids: None,
                ),
                edges=[],
            )

        self.assertEqual(drainer.calls, [(verification_id, "publisher failed")])

    def test_scheduler_failure_retries_drain_after_parent_is_terminal(self) -> None:
        verification_id = canonical_test_verification_id(
            f"execution-drain-retry:{self.test_id}"
        )
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        drain_calls: list[tuple[str, str]] = []

        class FlakyDrainer:
            def request_verification_cancel(
                self,
                runtime_verification_id: str,
                reason: str,
            ) -> dict[str, int]:
                drain_calls.append((runtime_verification_id, reason))
                if len(drain_calls) == 1:
                    raise RuntimeError("temporary drain failure")
                return {
                    "cancelled_cases": 1,
                    "awaiting_receipts": 0,
                    "affected_tasks": 1,
                    "affected_batches": 1,
                }

        execution_service = VerificationExecutionService(
            self.verification_service,
            self.verification_task_store,
            self.verification_task_completion_service,
            VerificationRuntimeRegistry(),
            FlakyDrainer(),
        )

        def _terminal_failure(row: dict[str, object]) -> TaskPublishResult:
            self.assertEqual(str(row["id"]), task_id)
            return TaskPublishResult(
                task_id=task_id,
                run_id="run-failed",
                judgehost_task_id="judgehost-failed",
                terminal_result=TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.FAILED,
                    run_id="run-failed",
                    judgehost_task_id="judgehost-failed",
                    result=make_execution_result(
                        verdict="FL",
                        error="source payload is unavailable",
                    ),
                    fail_reason="source payload is unavailable",
                ),
            )

        with self.assertRaisesRegex(
            VerificationCoordinatorFailure,
            "source payload is unavailable",
        ):
            execution_service.run(
                verification_id,
                callbacks=VerificationExecutionCallbacks(
                    publish_task=_terminal_failure,
                    probe_task_case_cache=lambda _task_ids: set(),
                    close_programs=lambda _program_ids: None,
                ),
                edges=[],
            )

        self.assertEqual(
            drain_calls,
            [
                (verification_id, "source payload is unavailable"),
                (verification_id, "source payload is unavailable"),
            ],
        )
        snapshot = self.verification_service.verification_snapshot(
            verification_id
        )
        assert snapshot is not None
        self.assertEqual(snapshot["record"]["status"], "failed")
