import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from app.service.execution.policy import normalize_execution_result
from app.service.judgehost.ports.case_binding import CaseBinding
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.workflow import _verification_required_file
from app.service.verification.judgehost_adapter import VerificationJudgehostAdapter
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.lifecycle import verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore
from app.service.verification.types import VerificationStatus, VerificationTaskStatus

from tests.identity_helpers import canonical_test_verification_id
from tests.isolated_db_helpers import isolated_db_fetch_all
from tests.verification_service_fixture import (
    VerificationServiceTestBase,
    make_execution_result,
    multi_pass_result,
    terminal_report,
)


class TestVerificationCompletionService(VerificationServiceTestBase):
    def test_completion_preparation_validates_equal_results_before_writing(self) -> None:
        for truncate in (False, True):
            with self.subTest(truncate=truncate):
                verification_id = canonical_test_verification_id(f"prepare-invalid:{self.test_id}:{truncate}")
                self._insert_verification_row(verification_id)
                task_ids = [verification_task_id(verification_id, "accepted", f"{i:03}.in") for i in (1, 2)]
                self._activate_graph(verification_id, tasks=[
                    {"id": task_id, "task_kind": "main-correct", "program_id": "accepted",
                     "source_path": "solutions/accepted.cpp", "test_name": f"{i:03}.in",
                     "expected_behavior": "accepted"}
                    for i, task_id in enumerate(task_ids, 1)
                ], edges=[])
                result = make_execution_result(
                    verdict="OK", output_ref=str(self.runtime_blob_store.put_bytes(b"output").blob_ref),
                    feedback="x" * (100_000 if truncate else 1),
                )
                invalid = replace(result, outcome=replace(
                    result.outcome, usage=replace(result.outcome.usage, memory_kb=1.0),
                ))
                self.assertEqual(result, invalid)
                with self.assertRaises(ValueError):
                    self.verification_task_store.commit_task_completions(tuple(
                        TaskCompletion(task_id=task_id, status=VerificationTaskStatus.DONE,
                                       run_id="", judgehost_task_id="", result=value)
                        for task_id, value in zip(task_ids, (result, invalid))
                    ))
                with self.db.conn() as conn:
                    rows = conn.execute(
                        "SELECT final_status FROM verification_tasks WHERE verification_id=?",
                        [verification_id],
                    ).fetchall()
                self.assertEqual([row[0] for row in rows], ["", ""])

    def test_shared_completion_result_preserves_bounded_and_distinct_feedback(self) -> None:
        verification_id = canonical_test_verification_id(f"prepare-shared:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_ids = [verification_task_id(verification_id, "accepted", f"{i:03}.in") for i in (1, 2, 3)]
        self._activate_graph(verification_id, tasks=[
            {"id": task_id, "task_kind": "main-correct", "program_id": "accepted",
             "source_path": "solutions/accepted.cpp", "test_name": f"{i:03}.in",
             "expected_behavior": "accepted"}
            for i, task_id in enumerate(task_ids, 1)
        ], edges=[])
        shared = make_execution_result(verdict="OK", feedback="你好" * 100_000)
        distinct = make_execution_result(verdict="OK", feedback="other case")
        commit = self.verification_task_store.commit_task_completions(tuple(
            TaskCompletion(task_id=task_id, status=VerificationTaskStatus.DONE,
                           run_id="", judgehost_task_id="", result=result)
            for task_id, result in zip(task_ids, (shared, shared, distinct))
        ))
        self.assertEqual(commit.committed_task_ids, frozenset(task_ids))
        rows = {row["id"]: row for row in self.verification_task_store.list_rows(verification_id)}
        self.assertEqual(rows[task_ids[0]]["result_json"], rows[task_ids[1]]["result_json"])
        self.assertLess(len(rows[task_ids[0]]["feedback_text"]), len(shared.feedback_text))
        self.assertEqual(rows[task_ids[2]]["feedback_text"], "other case")

    def test_solution_completions_progress_across_storage_and_publication_delays(self) -> None:
        for after_commit in (False, True):
            for replay in (False, True):
                with self.subTest(after_commit=after_commit, replay=replay):
                    verification_id = canonical_test_verification_id(
                        f"concurrent-solutions:{self.test_id}:{after_commit}:{replay}"
                    )
                    self._insert_verification_row(verification_id)
                    task_ids = [verification_task_id(verification_id, "solution-0", f"{i:03}.in")
                                for i in (1, 2)]
                    self._activate_graph(verification_id, tasks=[
                        {"id": task_id, "task_kind": "solution-run", "program_id": "solution-0",
                         "source_path": "solutions/accepted.cpp", "test_name": f"{i:03}.in",
                         "expected_behavior": "accepted"}
                        for i, task_id in enumerate(task_ids, 1)
                    ], edges=[])
                    store = self.verification_task_store
                    first = TaskCompletion(
                        task_id=task_ids[0], status=VerificationTaskStatus.DONE,
                        run_id="run-first", judgehost_task_id="jt-first",
                        result=normalize_execution_result(verdict="AC"),
                    )
                    self.assertTrue(store.bind_and_expose_judgehost_runtime(
                        first.task_id, expected_verification_id=verification_id,
                        expected_program_id="solution-0", expected_test_name="001.in",
                        run_id=first.run_id, judgehost_task_id=first.judgehost_task_id,
                        expose=lambda: None,
                    ))
                    second = TaskCompletion(
                        task_id=task_ids[0 if replay else 1], status=VerificationTaskStatus.DONE,
                        run_id="run-second", judgehost_task_id="jt-second",
                        result=normalize_execution_result(verdict="WA" if replay else "AC"),
                    )
                    entered, release = threading.Event(), threading.Event()
                    write_transaction = self.db.write_transaction
                    delayed_thread = None

                    def delayed_transaction(transaction):
                        if threading.current_thread() is not delayed_thread:
                            return write_transaction(transaction)
                        result = write_transaction(transaction) if after_commit else None
                        entered.set()
                        if not release.wait(timeout=5):
                            raise TimeoutError("completion was not released")
                        return result if after_commit else write_transaction(transaction)

                    def submit_first():
                        nonlocal delayed_thread
                        delayed_thread = threading.current_thread()
                        return store.commit_task_completions((first,))

                    with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            pending = pool.submit(submit_first)
                            try:
                                self.assertTrue(entered.wait(timeout=2))
                                other = pool.submit(store.commit_task_completions, (second,)).result(timeout=2)
                            finally:
                                release.set()
                            original = pending.result(timeout=2)
                    expected = "WA" if replay and not after_commit else "AC"
                    self.assertEqual(original.effective_completions[0].result.verdict, expected)
                    self.assertEqual(other.effective_completions[0].result.verdict,
                                     expected if replay else "AC")
                    self.assertEqual(store.runtime_row(first.task_id)["result"].verdict, expected)

    def test_cancel_during_solution_storage_or_publication_cannot_restore_binding(self) -> None:
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                verification_id = canonical_test_verification_id(f"solution-cancel:{self.test_id}:{after_commit}")
                self._insert_verification_row(verification_id)
                task_ids = [verification_task_id(verification_id, "solution-0", f"{i:03}.in") for i in (1, 2)]
                self._activate_graph(verification_id, tasks=[
                    {"id": task_id, "task_kind": "solution-run", "program_id": "solution-0",
                     "source_path": "solutions/accepted.cpp", "test_name": f"{i:03}.in",
                     "expected_behavior": "accepted"}
                    for i, task_id in enumerate(task_ids, 1)
                ], edges=[])
                store = self.verification_task_store
                self.assertTrue(store.bind_and_expose_judgehost_runtime(
                    task_ids[0], expected_verification_id=verification_id,
                    expected_program_id="solution-0", expected_test_name="001.in",
                    run_id="run", judgehost_task_id="jt", expose=lambda: None,
                ))
                completion = TaskCompletion(
                    task_id=task_ids[0], status=VerificationTaskStatus.DONE,
                    run_id="run", judgehost_task_id="jt", result=normalize_execution_result(verdict="AC"),
                )
                entered, release = threading.Event(), threading.Event()
                write_transaction = self.db.write_transaction
                delayed_thread = None

                def delayed_transaction(transaction):
                    if threading.current_thread() is not delayed_thread:
                        return write_transaction(transaction)
                    result = write_transaction(transaction) if after_commit else None
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("completion was not released")
                    return result if after_commit else write_transaction(transaction)

                def submit():
                    nonlocal delayed_thread
                    delayed_thread = threading.current_thread()
                    return store.commit_task_completions((completion,))

                with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        pending = pool.submit(submit)
                        try:
                            self.assertTrue(entered.wait(timeout=2))
                            cancelled = pool.submit(
                                store.transition_verification_terminal, verification_id,
                                status=VerificationStatus.CANCELLED, reason="cancel during publication",
                            ).result(timeout=2)
                            self.assertEqual(cancelled.outcome, "transitioned")
                            self.assertTrue(store.unbind_judgehost_runtime(task_ids[0], judgehost_task_id="jt"))
                            self.assertFalse(store.bind_and_expose_judgehost_runtime(
                                task_ids[0], expected_verification_id=verification_id,
                                expected_program_id="solution-0", expected_test_name="001.in",
                                run_id="during-publication", judgehost_task_id="during-publication",
                                expose=lambda: None,
                            ))
                        finally:
                            release.set()
                        committed = pending.result(timeout=2)
                expected = VerificationTaskStatus.DONE if after_commit else VerificationTaskStatus.CANCELLED
                self.assertEqual(committed.effective_completions[0].status, expected)
                self.assertIsNone(store.bound_task_context(task_ids[0]))
                self.assertFalse(store.bind_and_expose_judgehost_runtime(
                    task_ids[0], expected_verification_id=verification_id,
                    expected_program_id="solution-0", expected_test_name="001.in",
                    run_id="late", judgehost_task_id="late", expose=lambda: None,
                ))
                rows = {row["id"]: row for row in store.list_rows(verification_id)}
                self.assertEqual(rows[task_ids[0]]["status"], expected)
                self.assertEqual(rows[task_ids[1]]["status"], VerificationTaskStatus.CANCELLED)

    def test_input_owner_publication_coordinates_duplicates_without_blocking_other_verifications(self) -> None:
        verification_id = canonical_test_verification_id(f"concurrent-owners:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_ids = [verification_task_id(verification_id, "generator-0", f"{i:03}.in") for i in (1, 2)]
        self._activate_graph(verification_id, tasks=[
            {"id": task_id, "task_kind": "generate-input", "program_id": "generator-0",
             "source_path": "generators/gen.cpp", "test_name": f"{i:03}.in",
             "expected_behavior": "accepted"}
            for i, task_id in enumerate(task_ids, 1)
        ], edges=[])
        other_id = canonical_test_verification_id(f"other-owner-scope:{self.test_id}")
        other_task = self._activate_verification(
            verification_id=other_id, problem_id=self.problem_id, workspace_id=self.workspace_id,
        )
        ref = str(self.runtime_blob_store.put_bytes(b"identical input\n").blob_ref)
        completions = [TaskCompletion(
            task_id=task_id, status=VerificationTaskStatus.DONE, run_id=task_id,
            judgehost_task_id=task_id, result=make_execution_result(verdict="OK", output_ref=ref), input_ref=ref,
        ) for task_id in task_ids]
        store = self.verification_task_store
        entered, release = threading.Event(), threading.Event()
        write_transaction = self.db.write_transaction
        delayed_thread = None

        def delayed_transaction(transaction):
            result = write_transaction(transaction)
            if threading.current_thread() is delayed_thread:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("owner publication was not released")
            return result

        def submit_owner():
            nonlocal delayed_thread
            delayed_thread = threading.current_thread()
            return store.commit_task_completions((completions[0],))

        with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
            with ThreadPoolExecutor(max_workers=3) as pool:
                owner = pool.submit(submit_owner)
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    duplicate = pool.submit(store.commit_task_completions, (completions[1],))
                    other = pool.submit(store.commit_task_completions, (TaskCompletion(
                        task_id=other_task, status=VerificationTaskStatus.DONE,
                        run_id="other", judgehost_task_id="other",
                        result=normalize_execution_result(verdict="OK"),
                    ),)).result(timeout=2)
                    self.assertEqual(other.committed_task_ids, {other_task})
                finally:
                    release.set()
                self.assertEqual(owner.result(timeout=2).effective_completions[0].result.verdict, "OK")
                self.assertEqual(duplicate.result(timeout=2).effective_completions[0].result.verdict, "SK")
        rows = {row["id"]: row for row in store.list_rows(verification_id)}
        self.assertIn("same as 001.in", rows[task_ids[1]]["feedback_text"])

    def test_binding_reads_and_retirement_progress_while_completion_waits_for_storage(self) -> None:
        verification_id = canonical_test_verification_id(f"binding-progress:{self.test_id}")
        task_id = self._activate_verification(
            verification_id=verification_id,
            problem_id=self.problem_id,
            workspace_id=self.workspace_id,
        )
        store = self.verification_task_store
        self.assertTrue(store.bind_and_expose_judgehost_runtime(
            task_id, expected_verification_id=verification_id,
            expected_program_id="accepted", expected_test_name="001.in",
            run_id="run-progress", judgehost_task_id="jt-progress", expose=lambda: None,
        ))
        completion = TaskCompletion(
            task_id=task_id, status=VerificationTaskStatus.DONE,
            run_id="run-progress", judgehost_task_id="jt-progress",
            result=normalize_execution_result(verdict="OK"),
        )
        entered = threading.Event()
        release = threading.Event()
        write_transaction = self.db.write_transaction

        def delayed_transaction(transaction):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("storage was not released")
            return write_transaction(transaction)

        with patch.object(self.db, "write_transaction", side_effect=delayed_transaction):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(store.commit_task_completions, (completion,))
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    context = pool.submit(store.bound_task_context, task_id).result(timeout=2)
                    self.assertEqual(context["judgehost_task_id"], "jt-progress")
                    self.assertTrue(pool.submit(
                        store.unbind_judgehost_runtime, task_id,
                        judgehost_task_id="jt-progress",
                    ).result(timeout=2))
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=2).committed_task_ids, {task_id})
        self.assertIsNone(store.bound_task_context(task_id))
        self.assertEqual(store.list_rows(verification_id)[0]["status"], VerificationTaskStatus.DONE)

    def test_generated_input_owner_survives_rollback_and_runtime_rebuild(self) -> None:
        verification_id = canonical_test_verification_id(f"owner-rollback:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_ids = tuple(
            verification_task_id(verification_id, "generator-0", f"{i:03}.in")
            for i in range(1, 6)
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": f"{i:03}.in",
                    "expected_behavior": "accepted",
                    "status": VerificationTaskStatus.PENDING,
                }
                for i, task_id in enumerate(task_ids, start=1)
            ],
            edges=[],
        )
        ref = str(self.runtime_blob_store.put_bytes(b"same generated input\n").blob_ref)
        other_ref = str(self.runtime_blob_store.put_bytes(b"other generated input\n").blob_ref)
        completions = tuple(
            TaskCompletion(
                task_id=task_id,
                status=VerificationTaskStatus.DONE,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
                result=make_execution_result(
                    verdict="OK", output_ref=other_ref if i in (2, 3) else ref,
                ),
                input_ref=other_ref if i in (2, 3) else ref,
            )
            for i, task_id in enumerate(task_ids)
        )
        self._install_completion_ref_abort()
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced artifact ref failure"):
                self.verification_task_store.commit_task_completions((completions[0],))
        finally:
            self._clear_completion_ref_abort()
        self.verification_task_store.commit_task_completions((completions[1],))
        self._install_completion_ref_abort()
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced artifact ref failure"):
                self.verification_task_store.commit_task_completions((completions[2],))
        finally:
            self._clear_completion_ref_abort()
        self.verification_task_store.commit_task_completions((completions[3],))
        self.verification_task_store.commit_task_completions((completions[2],))
        self.verification_task_store.commit_task_completions((completions[0],))
        rebuilt = VerificationTaskStore(self.db)
        rebuilt.commit_task_completions((completions[4],))
        rows = {row["id"]: row for row in rebuilt.list_rows(verification_id)}
        self.assertEqual(rows[task_ids[1]]["verdict"], "OK")
        self.assertEqual(rows[task_ids[3]]["verdict"], "OK")
        self.assertEqual(rows[task_ids[2]]["verdict"], "SK")
        self.assertIn("same as 004.in", rows[task_ids[2]]["feedback_text"])
        for task_id in (task_ids[0], task_ids[4]):
            self.assertEqual(rows[task_id]["verdict"], "SK")
            self.assertIn("same as 002.in", rows[task_id]["feedback_text"])
            self.assertEqual(rows[task_id]["output_ref"], ref)

    def test_judgehost_adapter_requires_the_exact_durable_case_binding(self) -> None:
        verification_id = canonical_test_verification_id(f"binding:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                }
            ],
            edges=[],
        )
        adapter = VerificationJudgehostAdapter(
            self.db,
            self.verification_task_store,
            self.verification_task_completion_service,
            VerificationRuntimeRegistry(),
        )
        exposed: list[str] = []
        binding = CaseBinding(
            execution_scope_id=verification_id,
            program_id="generator-0",
            task_id=task_id,
            test_name="001.in",
        )
        self.assertTrue(
            adapter.bind_and_expose(
                (binding,),
                run_id="r-binding",
                judgehost_task_id="jt-binding",
                expose=lambda: exposed.append("exposed"),
            )
        )
        self.assertEqual(exposed, ["exposed"])
        self.assertTrue(adapter.unbind(task_id, judgehost_task_id="jt-binding"))
        for changed in (
            CaseBinding(
                execution_scope_id="ver-1",
                program_id=binding.program_id,
                task_id=binding.task_id,
                test_name=binding.test_name,
            ),
            CaseBinding(
                execution_scope_id=binding.execution_scope_id,
                program_id="other-program",
                task_id=binding.task_id,
                test_name=binding.test_name,
            ),
            CaseBinding(
                execution_scope_id=binding.execution_scope_id,
                program_id=binding.program_id,
                task_id=binding.task_id,
                test_name="002.in",
            ),
        ):
            with self.subTest(binding=changed):
                self.assertFalse(
                    adapter.bind_and_expose(
                        (changed,),
                        run_id="r-rejected",
                        judgehost_task_id="jt-rejected",
                        expose=lambda: exposed.append("invalid"),
                    )
                )
        self.assertEqual(exposed, ["exposed"])

    def test_prepare_generate_input_validator_rejection_sets_failure_reason(self) -> None:
        output_file = self.runtime_blob_store.put_bytes(b"bad-input\n")
        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-validator-reject",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "program_id": "generator-0",
        }
        execution_result = make_execution_result(
            verdict="WA",
            output_ref=str(output_file.blob_ref),
            feedback="validator rejected generated input\nline 2 detail",
        )
        final_result = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-generate",
                verification_id="ver-validator-reject",
                run_id="r-generate",
                result=execution_result,
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStatus.FAILED)
        self.assertEqual(final_result.verdict, "WA")
        self.assertEqual(
            final_result.fail_reason,
            "generate-input / generators/gen.cpp / 001.in: validator rejected generated input\nline 2 detail",
        )
        self.assertEqual(
            final_result.error_text, "validator rejected generated input\nline 2 detail"
        )
        self.assertEqual(
            final_result.feedback_text, "validator rejected generated input\nline 2 detail"
        )
        self.assertEqual(final_result.output_ref, output_file.blob_ref)

    def test_prepare_generate_input_truncation_does_not_set_input_ref(self) -> None:
        output_file = self.runtime_blob_store.put_bytes(
            b"50000 50000\n[output storage truncated after 65536 B]\n"
        )

        task_row = {
            "id": "vt-generate",
            "verification_id": "ver-truncated-generate",
            "task_kind": "generate-input",
            "source_path": "generators/gen.cpp",
            "test_name": "020.in",
            "judgehost_task_id": "jt-generate",
            "run_id": "r-generate",
            "program_id": "generator-0",
        }
        execution_result = make_execution_result(
            verdict="OK",
            output_ref=str(output_file.blob_ref),
            feedback="validator accepted",
        )
        final_result = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-generate",
                verification_id="ver-truncated-generate",
                run_id="r-generate",
                result=execution_result,
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStatus.FAILED)
        self.assertEqual(final_result.verdict, "FL")
        self.assertEqual(final_result.error_text, "generated input output was truncated for 020.in")
        self.assertEqual(
            final_result.feedback_text, "generated input output was truncated for 020.in"
        )
        self.assertEqual(final_result.output_ref, output_file.blob_ref)
        self.assertEqual(
            final_result.fail_reason,
            "generate-input / generators/gen.cpp / 020.in: generated input output was truncated for 020.in",
        )
        self.assertEqual(final_result.input_ref, "")

    def test_prepare_main_correct_preserves_canonical_compile_failure(self) -> None:
        task_row = {
            "id": "vt-main-correct",
            "verification_id": "ver-main-correct",
            "task_kind": "main-correct",
            "source_path": "solutions/std.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-main-correct",
            "run_id": "r-main-correct",
            "program_id": "accepted",
        }
        detailed_error = (
            "g++: internal compiler error: File size limit exceeded signal terminated program as\n"
            "Please submit a full bug report."
        )
        execution_result = make_execution_result(
            verdict="CE",
            error=detailed_error,
            compile_log=detailed_error,
            diagnostics=[{"level": "error", "message": detailed_error}],
        )
        final_result = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-main-correct",
                verification_id="ver-main-correct",
                run_id="r-main-correct",
                result=execution_result,
                status="failed",
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStatus.FAILED)
        self.assertEqual(final_result.verdict, "CE")
        self.assertEqual(final_result.error_text, detailed_error)
        self.assertEqual(final_result.compile_log, detailed_error)
        diagnostics_rows = final_result.result.compile.diagnostics
        self.assertEqual(diagnostics_rows[0]["message"], detailed_error)
        self.assertEqual(
            final_result.fail_reason,
            f"main-correct / solutions/std.cpp / 001.in: {detailed_error}",
        )

    def test_prepare_main_correct_re_is_a_hard_failure(self) -> None:
        task_row = {
            "id": "vt-main-re",
            "verification_id": "ver-main-re",
            "task_kind": "main-correct",
            "source_path": "solutions/std.cpp",
            "test_name": "001.in",
            "judgehost_task_id": "jt-main-re",
            "run_id": "r-main-re",
            "program_id": "accepted",
        }
        final_result = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-main-re",
                verification_id="ver-main-re",
                run_id="r-main-re",
                result=make_execution_result(
                    verdict="RE",
                    error="accepted solution crashed",
                ),
                status="ok",
                summary={"tests": [{"verdict": "RE"}]},
            ),
        )

        self.assertEqual(final_result.status, VerificationTaskStatus.FAILED)
        self.assertEqual(final_result.verdict, "RE")
        self.assertIn("accepted solution crashed", final_result.fail_reason)

    def test_expected_compile_error_is_a_complete_solution_decision(self) -> None:
        task_row = {
            "id": "vt-solution-ce",
            "verification_id": "ver-solution-ce",
            "task_kind": "solution-run",
            "source_path": "solutions/expected-ce.cpp",
            "test_name": "001.in",
            "expected_behavior": "rejected",
            "judgehost_task_id": "jt-solution-ce",
            "run_id": "r-solution-ce",
            "program_id": "solution-0",
        }
        compile_error = make_execution_result(
            verdict="CE",
            error="compiler rejected the source",
            compile_log="compiler rejected the source",
        )

        completion = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-solution-ce",
                verification_id="ver-solution-ce",
                run_id="r-solution-ce",
                result=compile_error,
                status="failed",
                summary={"error": "compile_error"},
            ),
        )

        self.assertEqual(completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(completion.verdict, "CE")
        self.assertEqual(completion.compile_log, "compiler rejected the source")
        self.assertEqual(completion.fail_reason, "")

    def test_expected_runtime_error_is_a_complete_solution_decision(self) -> None:
        task_row = {
            "id": "vt-solution-re",
            "verification_id": "ver-solution-re",
            "task_kind": "solution-run",
            "source_path": "solutions/expected-re.cpp",
            "test_name": "001.in",
            "expected_behavior": "run_time_error",
            "judgehost_task_id": "jt-solution-re",
            "run_id": "r-solution-re",
            "program_id": "solution-0",
        }
        runtime_error = make_execution_result(
            verdict="RE",
            error="process exited with status 1",
        )

        completion = self.verification_task_completion_service.prepare(
            task_row,
            terminal_report(
                judgehost_task_id="jt-solution-re",
                verification_id="ver-solution-re",
                run_id="r-solution-re",
                result=runtime_error,
                status="failed",
                summary={"tests": [{"verdict": "RE"}]},
            ),
        )

        self.assertEqual(completion.status, VerificationTaskStatus.DONE)
        self.assertEqual(completion.verdict, "RE")
        self.assertEqual(completion.error_text, "process exited with status 1")
        self.assertEqual(completion.fail_reason, "")

    def test_task_store_caps_frontend_display_fields(self) -> None:
        verification_id = canonical_test_verification_id(f"display-cap:{self.test_id}")
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
        oversized = "x" * 5000
        diagnostics_json = json.dumps(
            [{"level": "error", "message": "y" * 5000}], separators=(",", ":")
        )
        task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.FAILED,
                    run_id="r-cap",
                    judgehost_task_id="jt-cap",
                    result=normalize_execution_result(
                        verdict="CE",
                        answer_correct=True,
                        error=oversized,
                        feedback=oversized,
                        compile_log=oversized,
                        compile_diagnostics=json.loads(diagnostics_json),
                    ),
                    fail_reason=oversized,
                ),
            )
        )
        row = next(
            row for row in task_store.list_rows(verification_id) if str(row["id"]) == task_id
        )
        self.assertTrue(bool(row["answer_correct"]))
        limit = int(self.config_values.AUX_DISPLAY_TEXT_LIMIT_BYTES)
        for key in ("compile_log", "error_text", "feedback_text"):
            value = str(row[key] or "")
            self.assertLessEqual(len(value.encode("utf-8")), limit)
            self.assertTrue(value.endswith("..."))
        diagnostics_rows = row["result"].compile.diagnostics
        self.assertEqual(len(diagnostics_rows), 1)
        self.assertTrue(bool(diagnostics_rows[0].get("message_truncated")))
        self.assertLessEqual(
            len(str(diagnostics_rows[0].get("message") or "").encode("utf-8")), limit
        )

    def test_task_store_deduplicates_generated_content_and_skips_descendants(self) -> None:
        verification_id = canonical_test_verification_id(f"generated-dedup:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        owner_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        duplicate_id = verification_task_id(
            verification_id,
            "generator-0",
            "002.in",
        )
        main_id = verification_task_id(
            verification_id,
            "accepted",
            "002.in",
        )
        solution_id = verification_task_id(
            verification_id,
            "solution-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": owner_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                },
                {
                    "id": duplicate_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStatus.PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStatus.PENDING,
                },
                {
                    "id": solution_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 4,
                    "status": VerificationTaskStatus.PENDING,
                },
            ],
            edges=[
                (duplicate_id, main_id),
                (main_id, solution_id),
            ],
        )
        task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=owner_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-owner",
                    judgehost_task_id="jt-owner",
                    result=make_execution_result(
                        verdict="OK",
                        output_ref="blob://same-generated-input",
                    ),
                    input_ref="blob://same-generated-input",
                ),
            )
        )
        task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=duplicate_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-duplicate",
                    judgehost_task_id="jt-duplicate",
                    result=make_execution_result(
                        verdict="OK",
                        output_ref="blob://same-generated-input",
                    ),
                    input_ref="blob://same-generated-input",
                ),
            )
        )
        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(str(rows[owner_id]["verdict"]), "OK")
        self.assertEqual(str(rows[duplicate_id]["verdict"]), "SK")
        self.assertIn(
            "duplicate generated input; skipped",
            str(rows[duplicate_id]["feedback_text"]),
        )
        for task_id in (main_id, solution_id):
            self.assertEqual(str(rows[task_id]["status"]), VerificationTaskStatus.DONE)
            self.assertEqual(str(rows[task_id]["verdict"]), "SK")
            self.assertEqual(
                str(rows[task_id]["feedback_text"]),
                "skipped because generate-input was skipped",
            )
        shared_owners = isolated_db_fetch_all(
            self.db,
            """
            SELECT task_id,test_name,role
            FROM verification_task_artifacts
            WHERE verification_id=? AND artifact_ref=?
              AND role='generated-input'
            ORDER BY test_name
            """,
            [verification_id, "blob://same-generated-input"],
        )
        self.assertEqual(
            [
                (
                    str(item["task_id"]),
                    str(item["test_name"]),
                    str(item["role"]),
                )
                for item in shared_owners
            ],
            [
                (owner_id, "001.in", "generated-input"),
                (duplicate_id, "002.in", "generated-input"),
            ],
        )

    def test_mixed_completion_batch_deduplicates_only_generated_inputs(self) -> None:
        verification_id = canonical_test_verification_id(f"mixed-dedup:{self.test_id}")
        self._insert_verification_row(verification_id)
        owner_id = verification_task_id(verification_id, "generator-0", "001.in")
        duplicate_id = verification_task_id(verification_id, "generator-0", "002.in")
        main_id = verification_task_id(verification_id, "accepted", "001.in")
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": task_kind,
                    "source_path": source_path,
                    "program_id": program_id,
                    "test_name": test_name,
                    "expected_behavior": "accepted",
                }
                for task_id, task_kind, source_path, program_id, test_name in (
                    (owner_id, "generate-input", "generators/gen.cpp", "generator-0", "001.in"),
                    (duplicate_id, "generate-input", "generators/gen.cpp", "generator-0", "002.in"),
                    (main_id, "main-correct", "solutions/accepted.cpp", "accepted", "001.in"),
                )
            ],
            edges=[(owner_id, main_id)],
        )
        shared_ref = str(self.runtime_blob_store.put_bytes(b"1\n").blob_ref)
        completions = tuple(
            TaskCompletion(
                task_id=task_id,
                status=VerificationTaskStatus.DONE,
                run_id=f"r-{task_id}",
                judgehost_task_id=f"jt-{task_id}",
                result=make_execution_result(verdict="OK", output_ref=shared_ref),
                input_ref=shared_ref if task_id != main_id else "",
                answer_ref=shared_ref if task_id == main_id else "",
            )
            for task_id in (owner_id, main_id, duplicate_id)
        )
        published = []

        def read_published_artifacts(notified_id, _commit):
            for test_name, ref_key in (
                ("001.in", "input_ref"), ("002.in", "input_ref"), ("001.in", "answer_ref"),
            ):
                payload = _verification_required_file(
                    notified_id, test_name, ref_key, label=test_name,
                    verification_service=self.verification_service,
                    runtime_blob_store=self.runtime_blob_store,
                )
                self.assertEqual(payload.path.read_bytes(), b"1\n")
                published.append(payload)
            return True

        completion_service = VerificationTaskCompletionService(
            self.verification_task_store, self.runtime_blob_store, read_published_artifacts,
        )
        completion_service.commit(completions)
        self.assertEqual(len(published), 3)
        published[0].path.unlink()
        for test_name, expected in (
            ("missing.in", "artifact reference not published"),
            ("001.in", "published artifact blob unavailable"),
        ):
            with self.subTest(test_name=test_name), self.assertRaisesRegex(RuntimeError, expected):
                _verification_required_file(
                    verification_id, test_name, "input_ref", label=test_name,
                    verification_service=self.verification_service,
                    runtime_blob_store=self.runtime_blob_store,
                )
        retry = self.verification_task_store.commit_task_completions(completions)

        self.assertEqual(
            retry.already_terminal_task_ids,
            frozenset({owner_id, duplicate_id, main_id}),
        )
        rows = {
            str(row["id"]): row
            for row in self.verification_task_store.list_rows(verification_id)
        }
        self.assertEqual(
            {task_id: str(row["verdict"]) for task_id, row in rows.items()},
            {owner_id: "OK", main_id: "OK", duplicate_id: "SK"},
        )
        ownership = isolated_db_fetch_all(
            self.db,
            """
            SELECT task_id,role FROM verification_task_artifacts
            WHERE verification_id=? AND artifact_ref=?
              AND role IN ('generated-input','accepted-answer')
            """,
            [verification_id, shared_ref],
        )
        self.assertEqual(
            {(str(row["task_id"]), str(row["role"])) for row in ownership},
            {
                (owner_id, "generated-input"),
                (duplicate_id, "generated-input"),
                (main_id, "accepted-answer"),
            },
        )

    def test_completion_commit_persists_refs_failure_and_full_result_together(self) -> None:
        verification_id = canonical_test_verification_id(f"completion-evidence:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        generate_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        main_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": generate_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStatus.PENDING,
                },
            ],
            edges=[],
        )
        input_file = self.runtime_blob_store.put_bytes(b"generated input\n")
        answer_file = self.runtime_blob_store.put_bytes(b"correct answer\n")
        main_result = multi_pass_result(str(answer_file.blob_ref))

        commit = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=generate_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-generate-evidence",
                    judgehost_task_id="jt-generate-evidence",
                    result=make_execution_result(
                        verdict="OK",
                        output_ref=str(input_file.blob_ref),
                    ),
                    input_ref=str(input_file.blob_ref),
                ),
                TaskCompletion(
                    task_id=main_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-main-evidence",
                    judgehost_task_id="jt-main-evidence",
                    result=main_result,
                    answer_ref=str(answer_file.blob_ref),
                    fail_reason="first durable failure context",
                ),
            )
        )

        self.assertEqual(
            commit.committed_task_ids,
            frozenset({generate_id, main_id}),
        )
        refs = self.verification_service.verification_test_artifacts(verification_id)["001.in"]
        self.assertEqual(refs["input_ref"], input_file.blob_ref)
        self.assertEqual(refs["answer_ref"], answer_file.blob_ref)
        verification_row = self.verification_service.verification_record(verification_id)
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first durable failure context",
        )
        row = next(row for row in task_store.list_rows(verification_id) if row["id"] == main_id)
        persisted_result = row["result"]
        self.assertEqual(persisted_result.passes, main_result.passes)
        self.assertEqual(persisted_result.compile.log, main_result.compile.log)
        self.assertEqual(
            [item["message"] for item in persisted_result.compile.diagnostics],
            [item["message"] for item in main_result.compile.diagnostics],
        )
        self.assertEqual(persisted_result.warnings, main_result.warnings)
        self.assertEqual(persisted_result.outcome.usage, main_result.outcome.usage)
        ownership = isolated_db_fetch_all(
            self.db,
            """
            SELECT pass_number,role,artifact_ref,download_filename
            FROM verification_task_artifacts
            WHERE verification_id=? AND task_id=?
            ORDER BY pass_number,role
            """,
            [verification_id, main_id],
        )
        self.assertEqual(
            {(int(item["pass_number"]), str(item["role"])) for item in ownership},
            {
                (0, "accepted-answer"),
                *{
                    (pass_number, role)
                    for pass_number in (1, 2)
                    for role in (
                        "pass-compare-metadata",
                        "pass-feedback",
                        "pass-input",
                        "pass-metadata",
                        "pass-output",
                        "pass-stderr",
                        "pass-system",
                        "pass-team-feedback",
                    )
                },
            },
        )
        final_output = next(
            item
            for item in ownership
            if int(item["pass_number"]) == 2 and str(item["role"]) == "pass-output"
        )
        self.assertEqual(str(final_output["artifact_ref"]), answer_file.blob_ref)
        self.assertEqual(str(final_output["download_filename"]), "001.out")

    def test_completion_commit_rolls_back_task_refs_failure_and_memory_state(self) -> None:
        verification_id = canonical_test_verification_id(f"completion-rollback:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                }
            ],
            edges=[],
        )
        output_file = self.runtime_blob_store.put_bytes(b"generated input\n")
        self._install_completion_ref_abort()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "forced artifact ref failure",
            ):
                task_store.commit_task_completions(
                    (
                        TaskCompletion(
                            task_id=task_id,
                            status=VerificationTaskStatus.FAILED,
                            run_id="r-rollback",
                            judgehost_task_id="jt-rollback",
                            result=make_execution_result(
                                verdict="FL",
                                output_ref=str(output_file.blob_ref),
                                error="completion failed",
                            ),
                            input_ref=str(output_file.blob_ref),
                            fail_reason="completion failed",
                        ),
                    )
                )
        finally:
            self._clear_completion_ref_abort()

        row = next(
            row for row in task_store.list_rows(verification_id) if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStatus.PENDING)
        self.assertEqual(row["result"].verdict, "")
        self.assertEqual(
            self.verification_service.verification_test_artifacts(verification_id),
            {},
        )
        verification_row = self.verification_service.verification_record(verification_id)
        assert verification_row is not None
        self.assertEqual(str(verification_row["fail_reason"] or ""), "")

    def test_conflicting_completion_keeps_first_terminal_state_and_side_effects(self) -> None:
        verification_id = canonical_test_verification_id(f"completion-first-wins:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        task_id = verification_task_id(
            verification_id,
            "generator-0",
            "001.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                }
            ],
            edges=[],
        )
        first = TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStatus.DONE,
            run_id="r-first",
            judgehost_task_id="jt-first",
            result=make_execution_result(
                verdict="OK",
                output_ref="blob://first-output",
                feedback="first result",
            ),
            input_ref="blob://first-output",
            fail_reason="first failure context",
        )
        task_store.commit_task_completions((first,))
        retry = task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.FAILED,
                    run_id="r-conflict",
                    judgehost_task_id="jt-conflict",
                    result=make_execution_result(
                        verdict="FL",
                        output_ref="blob://conflicting-output",
                        feedback="conflicting result",
                    ),
                    input_ref="blob://conflicting-output",
                    fail_reason="conflicting failure context",
                ),
            )
        )

        self.assertEqual(retry.committed_task_ids, frozenset())
        self.assertEqual(
            retry.already_terminal_task_ids,
            frozenset({task_id}),
        )
        self.assertEqual(retry.effective_completions[0].verdict, "OK")
        row = next(
            row for row in task_store.list_rows(verification_id) if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStatus.DONE)
        self.assertEqual(row["verdict"], "OK")
        refs = self.verification_service.verification_test_artifacts(verification_id)["001.in"]
        self.assertEqual(refs["input_ref"], "blob://first-output")
        verification_row = self.verification_service.verification_record(verification_id)
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first failure context",
        )

    def test_late_diagnostic_preserves_terminal_evidence_and_failure(self) -> None:
        verification_id = canonical_test_verification_id(f"completion-diagnostic:{self.test_id}")
        self._insert_verification_row(verification_id)
        task_store = self.verification_task_store
        first_failure_id = verification_task_id(
            verification_id,
            "accepted",
            "001.in",
        )
        diagnostic_task_id = verification_task_id(
            verification_id,
            "generator-0",
            "002.in",
        )
        self._activate_graph(
            verification_id,
            tasks=[
                {
                    "id": first_failure_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 1,
                    "status": VerificationTaskStatus.PENDING,
                },
                {
                    "id": diagnostic_task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStatus.PENDING,
                },
            ],
            edges=[],
        )
        self.assertTrue(
            task_store.bind_and_expose_judgehost_runtime(
                diagnostic_task_id,
                expected_verification_id=verification_id,
                expected_program_id="generator-0",
                expected_test_name="002.in",
                run_id="r-amended",
                judgehost_task_id="jt-amended",
                expose=lambda: None,
            )
        )
        output_file = self.runtime_blob_store.put_bytes(b"generated input\n")
        original_result = make_execution_result(
            verdict="OK",
            output_ref=str(output_file.blob_ref),
            feedback="original feedback",
            compile_log="compile evidence",
            diagnostics=[{"level": "warning", "message": "kept"}],
        )
        task_store.commit_task_completions(
            (
                TaskCompletion(
                    task_id=first_failure_id,
                    status=VerificationTaskStatus.FAILED,
                    run_id="r-first-failure",
                    judgehost_task_id="jt-first-failure",
                    result=make_execution_result(
                        verdict="FL",
                        error="first task failed",
                    ),
                    fail_reason=(
                        "main-correct / solutions/accepted.cpp / 001.in: " "first task failed"
                    ),
                ),
                TaskCompletion(
                    task_id=diagnostic_task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="r-amended",
                    judgehost_task_id="jt-amended",
                    result=original_result,
                    input_ref=str(output_file.blob_ref),
                ),
            )
        )

        outcome = task_store.append_diagnostic(
            task_id=diagnostic_task_id,
            kind="debug-info",
            hostname="judgehost-1",
            text="late debug detail",
            received_at="2026-08-10T00:00:00+00:00",
        )

        self.assertEqual(outcome, "persisted")
        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        persisted = rows[diagnostic_task_id]
        self.assertEqual(persisted["status"], VerificationTaskStatus.DONE)
        self.assertEqual(persisted["verdict"], "OK")
        self.assertEqual(persisted["result"].passes, original_result.passes)
        self.assertEqual(
            persisted["result"].compile.log,
            original_result.compile.log,
        )
        self.assertEqual(
            [item["message"] for item in persisted["result"].compile.diagnostics],
            [item["message"] for item in original_result.compile.diagnostics],
        )
        self.assertEqual(persisted["result"].warnings, original_result.warnings)
        refs = self.verification_service.verification_test_artifacts(verification_id)["002.in"]
        self.assertEqual(refs["input_ref"], output_file.blob_ref)
        diagnostic = task_store.diagnostic_snapshot(diagnostic_task_id)
        self.assertEqual(len(diagnostic.items), 1)
        self.assertEqual(diagnostic.items[0].text, "late debug detail")
        self.assertEqual(
            task_store.append_diagnostic(
                task_id=diagnostic_task_id,
                kind="debug-info",
                hostname="judgehost-1",
                text="late debug detail",
                received_at="2026-08-10T00:00:01+00:00",
            ),
            "duplicate",
        )
        verification_row = self.verification_service.verification_record(verification_id)
        assert verification_row is not None
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "main-correct / solutions/accepted.cpp / 001.in: first task failed",
        )
