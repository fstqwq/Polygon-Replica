import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.service.execution.policy import normalize_execution_result
from app.service.verification.lifecycle import (
    ActivationPlan,
    PlannedTask,
    SanityFinish,
    verification_task_id,
)
from app.service.verification.task_completion import CompletionCommit, TaskCompletion
from app.service.verification.types import VerificationStatus, VerificationTaskStatus

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_all
from tests.verification_service_fixture import (
    VerificationServiceTestBase,
    VerificationTaskFixture,
    make_execution_result,
    terminal_report,
)


class TestVerificationLifecycleService(VerificationServiceTestBase):
    def test_cancel_waits_for_its_activation_or_fatal_publication_only(self) -> None:
        for fatal in (False, True):
            with self.subTest(fatal=fatal):
                verification_id = canonical_test_verification_id(f"scoped-publication:{self.test_id}:{fatal}")
                self._insert_verification_row(verification_id)
                task_id = verification_task_id(verification_id, "accepted", "001.in")
                plan = ActivationPlan.build(
                    verification_id, detail={},
                    programs=(self._verification_program(
                        program_id="accepted", kind="main-correct",
                        source_path="solutions/accepted.cpp", expected_behavior="accepted",
                    ),),
                    tasks=(PlannedTask(
                        task_id=task_id, predecessor_task_id=None, task_kind="main-correct",
                        source_path="solutions/accepted.cpp", program_id="accepted",
                        test_name="001.in", expected_behavior="accepted",
                    ),),
                )
                if fatal:
                    self.verification_service.activate_verification(plan)
                other_id = canonical_test_verification_id(f"scoped-other:{self.test_id}:{fatal}")
                other_task = self._activate_verification(
                    verification_id=other_id, problem_id=self.problem_id, workspace_id=self.workspace_id,
                )
                store = self.verification_task_store
                entered, release = threading.Event(), threading.Event()
                cancel_started = threading.Event()
                write_transaction = self.db.write_transaction
                delayed_thread = None

                def delayed_transaction(transaction):
                    result = write_transaction(transaction)
                    if threading.current_thread() is delayed_thread:
                        entered.set()
                        if not release.wait(timeout=5):
                            raise TimeoutError("lifecycle publication was not released")
                    return result

                def publish():
                    nonlocal delayed_thread
                    delayed_thread = threading.current_thread()
                    if fatal:
                        return store.commit_task_completions((TaskCompletion(
                            task_id=task_id, status=VerificationTaskStatus.FAILED,
                            run_id="failed", judgehost_task_id="failed",
                            result=normalize_execution_result(verdict="FL"), fail_reason="executor failed",
                        ),))
                    return self.verification_service.activate_verification(plan)

                def cancel():
                    cancel_started.set()
                    return store.transition_verification_terminal(
                        verification_id, status=VerificationStatus.CANCELLED, reason="user cancellation",
                    )

                with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        pending = pool.submit(publish)
                        try:
                            self.assertTrue(entered.wait(timeout=2))
                            cancelling = pool.submit(cancel)
                            self.assertTrue(cancel_started.wait(timeout=2))
                            repeated = pool.submit(cancel)
                            other = pool.submit(store.commit_task_completions, (TaskCompletion(
                                task_id=other_task, status=VerificationTaskStatus.DONE,
                                run_id="other", judgehost_task_id="other",
                                result=normalize_execution_result(verdict="OK"),
                            ),)).result(timeout=2)
                            self.assertEqual(other.committed_task_ids, {other_task})
                        finally:
                            release.set()
                        pending.result(timeout=2)
                        outcomes = {cancelling.result(timeout=2).outcome, repeated.result(timeout=2).outcome}
                self.assertEqual(outcomes, {"closed"} if fatal else {"transitioned", "closed"})
                self.assertFalse(store.bind_and_expose_judgehost_runtime(
                    task_id, expected_verification_id=verification_id,
                    expected_program_id="accepted", expected_test_name="001.in",
                    run_id="late", judgehost_task_id="late", expose=lambda: None,
                ))
                self.assertEqual(self.verification_service.verification_record(verification_id)["status"],
                                 "failed" if fatal else "cancelled")

    def test_cancellation_closes_admission_and_rollback_restores_it(self) -> None:
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                verification_id = canonical_test_verification_id(f"admission-cancel:{self.test_id}:{rollback}")
                task_id = self._activate_verification(
                    verification_id=verification_id,
                    problem_id=self.problem_id,
                    workspace_id=self.workspace_id,
                )
                store = self.verification_task_store

                def bind() -> bool:
                    return store.bind_and_expose_judgehost_runtime(
                        task_id, expected_verification_id=verification_id,
                        expected_program_id="accepted", expected_test_name="001.in",
                        run_id="run-admission", judgehost_task_id="jt-admission",
                        expose=lambda: None,
                    )

                self.assertTrue(bind())
                self.assertTrue(store.set_task_leased(task_id))
                entered = threading.Event()
                release = threading.Event()
                write_transaction = self.db.write_transaction

                def delayed_transaction(transaction):
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("cancellation storage was not released")
                    return write_transaction(transaction)

                binding_started = threading.Event()
                leasing_started = threading.Event()

                def concurrent_bind() -> bool:
                    binding_started.set()
                    return bind()

                def concurrent_lease() -> bool:
                    leasing_started.set()
                    return store.set_task_leased(task_id)

                if rollback:
                    self._install_verification_cancel_abort(verification_id)
                try:
                    with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            pending = pool.submit(
                                self.verification_service.cancel_verification,
                                verification_id, reason="cancel admission test",
                            )
                            try:
                                self.assertTrue(entered.wait(timeout=2))
                                binding = pool.submit(concurrent_bind)
                                leasing = pool.submit(concurrent_lease)
                                self.assertTrue(binding_started.wait(timeout=2))
                                self.assertTrue(leasing_started.wait(timeout=2))
                                with self.assertRaises(TimeoutError):
                                    binding.result(timeout=0.05)
                                with self.assertRaises(TimeoutError):
                                    leasing.result(timeout=0.05)
                            finally:
                                release.set()
                            if rollback:
                                with self.assertRaises(sqlite3.IntegrityError):
                                    pending.result(timeout=2)
                            else:
                                self.assertEqual(pending.result(timeout=2).outcome, "transitioned")
                            self.assertEqual(binding.result(timeout=2), rollback)
                            self.assertEqual(leasing.result(timeout=2), rollback)
                finally:
                    if rollback:
                        self._clear_verification_cancel_abort()
                self.assertEqual(bind(), rollback)
                self.assertEqual(store.set_task_leased(task_id), rollback)
                record = self.verification_service.verification_record(verification_id)
                self.assertEqual(record["status"], "running" if rollback else "cancelled")
                result = store.commit_task_completions((TaskCompletion(
                    task_id=task_id, status=VerificationTaskStatus.DONE,
                    run_id="run-admission", judgehost_task_id="jt-admission",
                    result=normalize_execution_result(verdict="OK"),
                ),))
                self.assertEqual(
                    result.effective_completions[0].status,
                    VerificationTaskStatus.DONE if rollback else VerificationTaskStatus.CANCELLED,
                )
                self.assertFalse(bind())
                self.assertFalse(store.set_task_leased(task_id))

    def test_fatal_completion_waiters_observe_commit_or_rollback(self) -> None:
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                verification_id = canonical_test_verification_id(f"fatal-admission:{self.test_id}:{rollback}")
                task_id = self._activate_verification(
                    verification_id=verification_id,
                    problem_id=self.problem_id, workspace_id=self.workspace_id,
                )
                store = self.verification_task_store

                def bind() -> bool:
                    return store.bind_and_expose_judgehost_runtime(
                        task_id, expected_verification_id=verification_id,
                        expected_program_id="accepted", expected_test_name="001.in",
                        run_id="run-fatal", judgehost_task_id="jt-fatal", expose=lambda: None,
                    )

                self.assertTrue(bind())
                entered = threading.Event()
                release = threading.Event()
                binding_started = threading.Event()
                write_transaction = self.db.write_transaction

                def concurrent_bind() -> bool:
                    binding_started.set()
                    return bind()

                def delayed_transaction(transaction):
                    def before_commit(conn):
                        result = transaction(conn)
                        entered.set()
                        if not release.wait(timeout=5):
                            raise TimeoutError("fatal completion was not released")
                        if rollback:
                            raise sqlite3.IntegrityError("forced completion rollback")
                        return result
                    return write_transaction(before_commit)

                with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        completion = pool.submit(store.commit_task_completions, (TaskCompletion(
                            task_id=task_id, status=VerificationTaskStatus.FAILED,
                            run_id="run-fatal", judgehost_task_id="jt-fatal",
                            result=normalize_execution_result(verdict="FL"), fail_reason="executor failed",
                        ),))
                        try:
                            self.assertTrue(entered.wait(timeout=2))
                            binding = pool.submit(concurrent_bind)
                            self.assertTrue(binding_started.wait(timeout=2))
                            with self.assertRaises(TimeoutError):
                                binding.result(timeout=0.05)
                        finally:
                            release.set()
                        if rollback:
                            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced completion rollback"):
                                completion.result(timeout=2)
                        else:
                            self.assertEqual(completion.result(timeout=2).parent_transition, "failed")
                        self.assertEqual(binding.result(timeout=2), rollback)
                self.assertEqual(store.set_task_leased(task_id), rollback)
                self.assertEqual(
                    self.verification_service.verification_record(verification_id)["status"],
                    "running" if rollback else "failed",
                )

    def test_other_task_can_be_published_and_leased_during_completion_storage(self) -> None:
        verification_id = canonical_test_verification_id(f"admission-progress:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_ids = tuple(verification_task_id(verification_id, "accepted", name) for name in ("001.in", "002.in"))
        self._activate_graph(verification_id, tasks=[
            {"id": task_id, "task_kind": "main-correct", "program_id": "accepted",
             "source_path": "solutions/ac.cpp", "expected_behavior": "accepted", "test_name": name}
            for task_id, name in zip(task_ids, ("001.in", "002.in"))
        ], edges=[])
        store = self.verification_task_store
        entered = threading.Event()
        release = threading.Event()
        write_transaction = self.db.write_transaction

        def delayed_transaction(transaction):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("completion storage was not released")
            return write_transaction(transaction)

        with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(store.commit_task_completions, (TaskCompletion(
                    task_id=task_ids[0], status=VerificationTaskStatus.DONE,
                    run_id="r-first", judgehost_task_id="jt-first",
                    result=normalize_execution_result(verdict="OK"),
                ),))
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    self.assertTrue(pool.submit(
                        store.bind_and_expose_judgehost_runtime, task_ids[1],
                        expected_verification_id=verification_id,
                        expected_program_id="accepted", expected_test_name="002.in",
                        run_id="r-next", judgehost_task_id="jt-next", expose=lambda: None,
                    ).result(timeout=2))
                    self.assertTrue(pool.submit(store.set_task_leased, task_ids[1]).result(timeout=2))
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=2).committed_task_ids, {task_ids[0]})
        self.assertEqual([row["status"] for row in store.list_rows(verification_id)], [
            VerificationTaskStatus.DONE, VerificationTaskStatus.LEASED,
        ])

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

        def _accepted_task(verification_id: str) -> VerificationTaskFixture:
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
                    "source_path": "solutions/tle-or-re.cpp",
                    "program_id": "solution-0",
                    "test_name": "001.in",
                    "expected_behavior": "tle_or_re",
                    "status": VerificationTaskStatus.QUEUED,
                },
                {
                    "id": second_task_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/tle-or-re.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "tle_or_re",
                    "status": VerificationTaskStatus.QUEUED,
                },
            ],
            edges=[],
        )

        first_completion, first_commit = self._commit_solution_result(
            verification_id,
            first_task_id,
            "AC",
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
            "RE",
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
                    expected_verification_id=verification_id,
                    expected_program_id=(
                        "solution-0" if task_id == accepted_id else "solution-1"
                    ),
                    expected_test_name="001.in",
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
                    "status": VerificationTaskStatus.LEASED,
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
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
        record = self.verification_service.verification_record(verification_id)
        assert record is not None
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["fail_reason"], "verification cancelled by user")

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
                    "status": VerificationTaskStatus.LEASED,
                },
                {
                    "id": pending_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/a.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
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
