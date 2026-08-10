from __future__ import annotations

import sqlite3
import threading

from app.service.verification.execution_result import normalize_execution_result
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    SanityFinish,
    verification_task_id,
)
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_all
from tests.verification_service_fixture import (
    VerificationServiceTestBase,
    make_execution_result,
    terminal_report,
)


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
        self.assertEqual(str(row["status"]), "failed")
        task_rows = self.verification_task_store.list_rows(verification_id)
        if outcomes["activate"] == "activated":
            self.assertEqual(len(task_rows), 1)
            self.assertEqual(
                str(task_rows[0]["status"]),
                VerificationTaskStore.TASK_CANCELLED,
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
            status=VerificationTaskStore.TASK_DONE,
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
            self.assertEqual(task["status"], VerificationTaskStore.TASK_DONE)
        else:
            self.assertEqual(str(parent["status"]), "failed")
            self.assertEqual(outcomes, {
                "completion": "already-terminal",
                "cancel": "transitioned",
            })
            self.assertEqual(
                task["status"],
                VerificationTaskStore.TASK_CANCELLED,
            )
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStore.TASK_DONE,
                    VerificationTaskStore.TASK_FAILED,
                    VerificationTaskStore.TASK_CANCELLED,
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
                    status=VerificationTaskStore.TASK_DONE,
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
            self.assertEqual(str(parent["status"]), "failed")
            self.assertEqual(outcomes, {
                "finish": "closed",
                "cancel": "transitioned",
            })
            self.assertEqual(str(parent["sanity_status"]), "skipped")
        self.assertTrue(
            all(
                row["status"]
                in {
                    VerificationTaskStore.TASK_DONE,
                    VerificationTaskStore.TASK_FAILED,
                    VerificationTaskStore.TASK_CANCELLED,
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
        failed_id = canonical_test_verification_id(
            f"invariant-failed:{self.test_id}"
        )
        for verification_id in (queued_id, running_id, ok_id, failed_id):
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
                    status=VerificationTaskStore.TASK_DONE,
                    run_id="run-invariant-ok",
                    judgehost_task_id="judgehost-invariant-ok",
                    result=normalize_execution_result(verdict="OK"),
                ),
            )
        )
        failed = self.verification_service.cancel_verification(
            failed_id,
            reason="invariant terminal fixture",
        )
        self.assertEqual(failed.outcome, "transitioned")

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
            WHERE status IN ('ok','failed')
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
        self.assertEqual(mismatch.status, VerificationTaskStore.TASK_FAILED)
        self.assertIn("required=[AC]", mismatch.fail_reason)
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
        self.assertEqual(matched.status, VerificationTaskStore.TASK_DONE)
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
                    "status": VerificationTaskStore.TASK_LEASED,
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
                    "status": VerificationTaskStore.TASK_PENDING,
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
                    status=VerificationTaskStore.TASK_DONE,
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
            VerificationTaskStore.TASK_CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )

    def test_cancel_persists_first_failure_and_terminalizes_task(self) -> None:
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
                    "status": VerificationTaskStore.TASK_PENDING,
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
        self.assertEqual(str(row["status"]), "failed")
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
            VerificationTaskStore.TASK_CANCELLED,
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
                    "status": VerificationTaskStore.TASK_LEASED,
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
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
            detail={"mode": "pass-fail"},
        )

        summary = self.verification_service.recover_startup(
            reason="cancelled on service startup"
        )

        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(summary.verification_ids, (verification_id,))
        self.assertEqual(
            str(rows[running_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
        )
        self.assertEqual(
            str(rows[pending_id]["status"]),
            VerificationTaskStore.TASK_CANCELLED,
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
            reason="cancelled on service startup"
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
            "cancelled on service startup",
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
        self.assertEqual(
            str(row["fail_reason"]),
            "generate-input / generators/gen.cpp / 001.in: validator failed",
        )
