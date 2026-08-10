from __future__ import annotations

import json
import sqlite3

from app.service.verification.execution_result import normalize_execution_result
from app.service.verification.lifecycle import verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.task_store import VerificationTaskStore

from tests.identity_helpers import canonical_test_verification_id
from tests.verification_service_fixture import (
    VerificationServiceTestBase,
    make_execution_result,
    multi_pass_result,
    terminal_report,
)


class TestVerificationCompletionService(VerificationServiceTestBase):
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

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "WA")
        self.assertEqual(
            final_result.fail_reason,
            "generate-input / generators/gen.cpp / 001.in: validator rejected generated input\nline 2 detail",
        )
        self.assertEqual(final_result.error_text, "validator rejected generated input\nline 2 detail")
        self.assertEqual(final_result.feedback_text, "validator rejected generated input\nline 2 detail")
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

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "FL")
        self.assertEqual(final_result.error_text, "generated input output was truncated for 020.in")
        self.assertEqual(final_result.feedback_text, "generated input output was truncated for 020.in")
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

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
        self.assertEqual(final_result.verdict, "CE")
        self.assertEqual(final_result.error_text, detailed_error)
        self.assertEqual(final_result.compile_log, detailed_error)
        diagnostics_rows = json.loads(final_result.diagnostics_json)
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

        self.assertEqual(final_result.status, VerificationTaskStore.TASK_FAILED)
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

        self.assertEqual(completion.status, VerificationTaskStore.TASK_DONE)
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

        self.assertEqual(completion.status, VerificationTaskStore.TASK_DONE)
        self.assertEqual(completion.verdict, "RE")
        self.assertEqual(completion.error_text, "process exited with status 1")
        self.assertEqual(completion.fail_reason, "")

    def test_task_store_caps_frontend_display_fields(self) -> None:
        verification_id = canonical_test_verification_id(
            f"display-cap:{self.test_id}"
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
        oversized = "x" * 5000
        diagnostics_json = json.dumps([{"level": "error", "message": "y" * 5000}], separators=(",", ":"))
        task_store.commit_task_completions((TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStore.TASK_FAILED,
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
        ),))
        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertTrue(bool(row["answer_correct"]))
        limit = int(getattr(self.constants, "AUX_DISPLAY_TEXT_LIMIT_BYTES", 2048) or 2048)
        for key in ("compile_log", "error_text", "feedback_text"):
            value = str(row[key] or "")
            self.assertLessEqual(len(value.encode("utf-8")), limit)
            self.assertTrue(value.endswith("..."))
        diagnostics_rows = json.loads(str(row["diagnostics_json"] or "[]"))
        self.assertEqual(len(diagnostics_rows), 1)
        self.assertTrue(bool(diagnostics_rows[0].get("message_truncated")))
        self.assertLessEqual(len(str(diagnostics_rows[0].get("message") or "").encode("utf-8")), limit)

    def test_task_store_deduplicates_generated_content_and_skips_descendants(self) -> None:
        verification_id = canonical_test_verification_id(
            f"generated-dedup:{self.test_id}"
        )
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
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": duplicate_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 3,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": solution_id,
                    "task_kind": "solution-run",
                    "source_path": "solutions/wa.cpp",
                    "program_id": "solution-0",
                    "test_name": "002.in",
                    "expected_behavior": "wrong_answer",
                    "queue_index": 4,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[
                (duplicate_id, main_id),
                (main_id, solution_id),
            ],
        )
        task_store.commit_task_completions((TaskCompletion(
            task_id=owner_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-owner",
            judgehost_task_id="jt-owner",
            result=make_execution_result(
                verdict="OK",
                output_ref="blob://same-generated-input",
            ),
            input_ref="blob://same-generated-input",
        ),))
        task_store.commit_task_completions((TaskCompletion(
            task_id=duplicate_id,
            status=VerificationTaskStore.TASK_DONE,
            run_id="r-duplicate",
            judgehost_task_id="jt-duplicate",
            result=make_execution_result(
                verdict="OK",
                output_ref="blob://same-generated-input",
            ),
            input_ref="blob://same-generated-input",
        ),))
        rows = {str(row["id"]): row for row in task_store.list_rows(verification_id)}
        self.assertEqual(str(rows[owner_id]["verdict"]), "OK")
        self.assertEqual(str(rows[duplicate_id]["verdict"]), "SK")
        self.assertIn(
            "duplicate generated input; skipped",
            str(rows[duplicate_id]["feedback_text"]),
        )
        for task_id in (main_id, solution_id):
            self.assertEqual(str(rows[task_id]["status"]), VerificationTaskStore.TASK_DONE)
            self.assertEqual(str(rows[task_id]["verdict"]), "SK")
            self.assertEqual(
                str(rows[task_id]["feedback_text"]),
                "skipped because generate-input was skipped",
            )

    def test_completion_commit_persists_refs_failure_and_full_result_together(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-evidence:{self.test_id}"
        )
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
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": main_id,
                    "task_kind": "main-correct",
                    "source_path": "solutions/accepted.cpp",
                    "program_id": "accepted",
                    "test_name": "001.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
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
                    status=VerificationTaskStore.TASK_DONE,
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
                    status=VerificationTaskStore.TASK_DONE,
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
        refs = self.verification_service.verification_artifact_refs(
            verification_id
        )["001.in"]
        self.assertEqual(refs["input_ref"], input_file.blob_ref)
        self.assertEqual(refs["answer_ref"], answer_file.blob_ref)
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first durable failure context",
        )
        row = next(
            row
            for row in task_store.list_rows(verification_id)
            if row["id"] == main_id
        )
        persisted_result = row["result"]
        self.assertEqual(persisted_result.passes, main_result.passes)
        self.assertEqual(persisted_result.compile.log, main_result.compile.log)
        self.assertEqual(
            [item["message"] for item in persisted_result.compile.diagnostics],
            [item["message"] for item in main_result.compile.diagnostics],
        )
        self.assertEqual(persisted_result.warnings, main_result.warnings)
        self.assertEqual(persisted_result.outcome.usage, main_result.outcome.usage)

    def test_completion_commit_rolls_back_task_refs_failure_and_memory_state(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-rollback:{self.test_id}"
        )
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
                    "status": VerificationTaskStore.TASK_PENDING,
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
                            status=VerificationTaskStore.TASK_FAILED,
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
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStore.TASK_PENDING)
        self.assertEqual(row["result"].verdict, "")
        self.assertEqual(
            self.verification_service.verification_artifact_refs(verification_id),
            {},
        )
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["fail_reason"] or ""), "")

    def test_conflicting_completion_keeps_first_terminal_state_and_side_effects(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-first-wins:{self.test_id}"
        )
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
                    "status": VerificationTaskStore.TASK_PENDING,
                }
            ],
            edges=[],
        )
        first = TaskCompletion(
            task_id=task_id,
            status=VerificationTaskStore.TASK_DONE,
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
                    status=VerificationTaskStore.TASK_FAILED,
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
            row
            for row in task_store.list_rows(verification_id)
            if str(row["id"]) == task_id
        )
        self.assertEqual(row["status"], VerificationTaskStore.TASK_DONE)
        self.assertEqual(row["verdict"], "OK")
        refs = self.verification_service.verification_artifact_refs(
            verification_id
        )["001.in"]
        self.assertEqual(refs["input_ref"], "blob://first-output")
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(str(verification_row["status"]), "failed")
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "first failure context",
        )

    def test_late_diagnostic_preserves_terminal_evidence_and_failure(self) -> None:
        verification_id = canonical_test_verification_id(
            f"completion-diagnostic:{self.test_id}"
        )
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
                    "status": VerificationTaskStore.TASK_PENDING,
                },
                {
                    "id": diagnostic_task_id,
                    "task_kind": "generate-input",
                    "source_path": "generators/gen.cpp",
                    "program_id": "generator-0",
                    "test_name": "002.in",
                    "expected_behavior": "accepted",
                    "queue_index": 2,
                    "status": VerificationTaskStore.TASK_PENDING,
                },
            ],
            edges=[],
        )
        self.assertTrue(
            task_store.bind_and_expose_judgehost_runtime(
                diagnostic_task_id,
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
                    status=VerificationTaskStore.TASK_FAILED,
                    run_id="r-first-failure",
                    judgehost_task_id="jt-first-failure",
                    result=make_execution_result(
                        verdict="FL",
                        error="first task failed",
                    ),
                    fail_reason=(
                        "main-correct / solutions/accepted.cpp / 001.in: "
                        "first task failed"
                    ),
                ),
                TaskCompletion(
                    task_id=diagnostic_task_id,
                    status=VerificationTaskStore.TASK_DONE,
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
        rows = {
            str(row["id"]): row
            for row in task_store.list_rows(verification_id)
        }
        persisted = rows[diagnostic_task_id]
        self.assertEqual(persisted["status"], VerificationTaskStore.TASK_DONE)
        self.assertEqual(persisted["verdict"], "OK")
        self.assertEqual(persisted["result"].passes, original_result.passes)
        self.assertEqual(
            persisted["result"].compile.log,
            original_result.compile.log,
        )
        self.assertEqual(
            [
                item["message"]
                for item in persisted["result"].compile.diagnostics
            ],
            [item["message"] for item in original_result.compile.diagnostics],
        )
        self.assertEqual(persisted["result"].warnings, original_result.warnings)
        refs = self.verification_service.verification_artifact_refs(
            verification_id
        )["002.in"]
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
        verification_row = self.verification_service.verification_record(
            verification_id
        )
        assert verification_row is not None
        self.assertEqual(
            str(verification_row["fail_reason"]),
            "main-correct / solutions/accepted.cpp / 001.in: first task failed",
        )
