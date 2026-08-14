import hashlib
import json
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.service.judgehost.domjudge.case_result import build_case_result
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.model import (
    CaseReportTelemetry,
    CompileSubmission,
    ExecutionBatchRow,
    ExecutionBatchSpec,
    JudgehostCaseRow,
)
from app.service.judgehost.domjudge.identity import submit_id
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.rwlock import WriterPriorityRWLock

_NOW = "2026-07-29T00:00:00+00:00"
_HASH = "1" * 64
_COMPILE_KEY = "5" * 64


def _compile_submission() -> CompileSubmission:
    return CompileSubmission(
        compile_key=_COMPILE_KEY,
        submit_id=submit_id(_COMPILE_KEY),
        source_name="solution.cpp",
        source_file=PayloadFile(
            path=Path("/tmp/test-solution.cpp"),
            size=25,
            identity=hashlib.sha256(b"int main() { return 0; }\n").hexdigest(),
        ),
        extra_source_items=(),
        compile_files=(),
    )


def _result(test_name: str, *, runresult: str = "correct", verdict: str = "OK"):
    return build_case_result(
        test_name=test_name,
        runresult=runresult,
        verdict=verdict,
        runtime_sec=0.001,
        cpu_sec=0.001,
        wall_sec=0.002,
        memory_kb=1024,
        score_text="",
        output_run_ref="",
        output_error_ref="",
        output_system_ref="",
        output_diff_ref="",
        metadata_ref="",
        compare_metadata_ref="",
        team_message_ref="",
        feedback_text="",
        feedback_files=[],
        answer_correct=False,
    )


def _receipt_generation(store: JudgehostBatchRuntime, case_id: int) -> int:
    receipt = store.acquire_case_callback_receipt(case_id)
    if receipt is None:
        raise AssertionError(f"case {case_id} has no callback identity")
    store.release_case_callback_receipt(receipt.receipt_id)
    return receipt.claim_generation


def _finish_pending_case(
    store: JudgehostBatchRuntime, batch_id: int, test_name: str
) -> None:
    case = next(
        row for row in store.cases_for_batch(batch_id) if row["test_name"] == test_name
    )
    hostname = f"host-{test_name}"
    leased = _lease_cases(
        store,
        batch_id,
        hostname=hostname,
        limit=1,
        now_text=_NOW,
    )[0]
    assert int(leased["id"]) == int(case["id"])
    claim = store.claim_case_reporting(
        int(case["id"]),
        hostname=hostname,
        receipt_generation=_receipt_generation(store, int(case["id"])),
        now_text=_NOW,
    )
    assert claim is not None
    outcome = store.commit_case_result(
        claim.case_id,
        generation=claim.generation,
        result=_result(test_name),
        updated_at=_NOW,
    )
    assert outcome == "reported"


def _case_row(
    task_id: str,
    run_id: str,
    test_name: str,
    ordinal: int,
    *,
    status: str = "pending",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "test_name": test_name,
        "ordinal": ordinal,
        "scope_sequence": 1,
        "testcase_id": None,
        "testcase_hash": _HASH,
        "testcase_input_hash": _HASH,
        "testcase_answer_hash": _HASH,
        "input_ref": "",
        "answer_ref": "",
        "status": status,
    }


def _create_batch(
    store: JudgehostBatchRuntime,
    *,
    task_id: str,
    run_id: str,
    case_rows: list[dict[str, object]],
    execution_signature: str | None = None,
    verification_program_id: str | None = None,
) -> int:
    signature = hashlib.sha256(
        (execution_signature or f"signature-{task_id}").encode("utf-8")
    ).hexdigest()
    batch_id = store.create_batch_with_cases(
        task_id=task_id,
        run_id=run_id,
        verification_program_id=(
            verification_program_id
            if verification_program_id is not None
            else f"program-{task_id}"
        ),
        execution_signature=signature,
        task_kind="solution-run",
        verification_id="ver-1",
        compile_key=_COMPILE_KEY,
        compile_submission=_compile_submission(),
        contest_id="default",
        mode="pass-fail",
        source_name="solution.cpp",
        compile_hash="2" * 32,
        run_hash="3" * 32,
        compare_hash="4" * 32,
        source_hash=_HASH,
        compile_config_json="{}",
        run_config_json="{}",
        compare_config_json="{}",
        expected_behavior="accepted",
        verification_source="run.execute",
        bypass_case_result_cache=0,
        service_class="background",
        batch_spec=ExecutionBatchSpec(),
        created_at=_NOW,
        case_rows=case_rows,
    )
    if store.fetch_batch(batch_id)["materialization_state"] == "unmaterialized":
        claim = store.claim_materialization(batch_id, now_text=_NOW)
        assert claim is not None
        materialized_submission = replace(
            claim.submission,
            source_file=replace(
                claim.submission.source_file,
                blob_ref=f"blob://sha256/{claim.submission.source_file.identity}",
            ),
            extra_source_items=tuple(
                (
                    name,
                    replace(
                        payload,
                        blob_ref=f"blob://sha256/{payload.identity}",
                    ),
                )
                for name, payload in claim.submission.extra_source_items
            ),
        )
        assert store.finish_materialization(
            claim,
            success=True,
            materialized_submission=materialized_submission,
            error_text="",
            now_text=_NOW,
        )
    return batch_id


def _lease_cases(
    store: JudgehostBatchRuntime,
    batch_id: int,
    *,
    hostname: str,
    limit: int,
    now_text: str,
) -> list[JudgehostCaseRow]:
    claim = store.claim_lease(
        batch_id,
        hostname=hostname,
        limit=limit,
        now_text=now_text,
    )
    if claim is None:
        return []
    assert store.commit_lease(claim)
    return list(claim.cases)


class TestJudgehostBatchRuntimeLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JudgehostBatchRuntime(id_base=100)

    def test_verification_cancel_preserves_leased_case_until_receipt(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-cancel-receipt",
            run_id="run-cancel-receipt",
            case_rows=[
                _case_row("task-cancel-receipt", "run-cancel-receipt", "001.in", 1),
                _case_row("task-cancel-receipt", "run-cancel-receipt", "002.in", 2),
            ],
        )
        leased = _lease_cases(
            self.store,
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=_NOW,
        )
        self.assertEqual(len(leased), 1)
        leased_case_id = int(leased[0]["id"])
        self.store.record_batch_leased(
            "host-a",
            batch_id,
            [leased_case_id],
            leased_monotonic=10.0,
        )

        cancellation = self.store.request_verification_cancel("ver-1", now_text=_NOW)
        self.assertEqual(cancellation.cancelled_case_count, 1)
        self.assertEqual(cancellation.awaiting_receipt_count, 1)
        rows = {
            str(row["test_name"]): row for row in self.store.cases_for_batch(batch_id)
        }
        self.assertEqual(rows["001.in"]["status"], "leased")
        self.assertEqual(rows["001.in"]["lease_owner"], "host-a")
        self.assertTrue(rows["001.in"]["cancel_requested"])
        self.assertEqual(rows["002.in"]["status"], "cancelled")
        self.assertEqual(
            _lease_cases(
                self.store, batch_id, hostname="host-b", limit=2, now_text=_NOW
            ),
            [],
        )

        claim = self.store.claim_case_reporting(
            leased_case_id,
            hostname="host-a",
            receipt_generation=_receipt_generation(
                self.store,
                leased_case_id,
            ),
            now_text=_NOW,
        )
        self.assertIsNotNone(claim)
        accepted = self.store.commit_cancelled_receipt(
            leased_case_id,
            generation=claim.generation,
            updated_at=_NOW,
            report_telemetry=CaseReportTelemetry(
                hostname="host-a",
                reported_at="2026-07-29T00:00:02+00:00",
                reported_monotonic=12.0,
                verification_id="ver-1",
                problem_slug="alice/sample",
                task_kind="solution-run",
                source_label="solution.cpp",
                test_name="001.in",
            ),
        )
        self.assertTrue(accepted)
        self.assertEqual(self.store.fetch_case(leased_case_id)["status"], "cancelled")
        telemetry = self.store.host_telemetry_snapshot()["host-a"]
        self.assertEqual(telemetry["judged_case_count"], 1)
        self.assertEqual(telemetry["recent_avg_per_case_sec"], 2.0)

    def test_execution_batch_closes_when_program_finishes(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-append-first",
            run_id="run-append-first",
            case_rows=[_case_row("task-append-first", "run-append-first", "001.in", 1)],
            execution_signature="shared-signature",
            verification_program_id="solution-0",
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        self.assertEqual(self.store.fetch_batch(batch_id)["status"], "open")
        publication_claim = self.store.claim_batch_finalization(
            batch_id,
            now_text=_NOW,
        )
        self.assertIsNotNone(publication_claim)
        assert publication_claim is not None
        self.assertFalse(publication_claim.terminal_transition)
        first_case_id = int(self.store.cases_for_batch(batch_id)[0]["id"])
        self.assertTrue(self.store.acknowledge_case_completion(first_case_id))
        self.assertTrue(self.store.complete_batch_finalization(publication_claim))
        self.assertEqual(self.store.due_batch_finalizations(limit=1), [])

        same_batch_id = _create_batch(
            self.store,
            task_id="task-later",
            run_id="run-later",
            case_rows=[_case_row("task-later", "run-later", "002.in", 1)],
            execution_signature="shared-signature",
            verification_program_id="solution-0",
        )
        self.assertEqual(same_batch_id, batch_id)
        self.assertTrue(self.store.activate_task_cases("task-later", now_text=_NOW))
        later_case_id = int(self.store.cases_for_task("task-later")[0]["id"])
        cache_claims = self.store.claim_cache_cases(
            batch_id,
            hostname="cache-setup",
            limit=1,
            now_text=_NOW,
        )
        self.assertEqual(
            [claim.case_id for claim, _row in cache_claims],
            [later_case_id],
        )
        self.assertTrue(
            self.store.finish_cache_miss(
                later_case_id,
                generation=cache_claims[0][0].generation,
                updated_at=_NOW,
            )
        )
        _finish_pending_case(self.store, batch_id, "002.in")
        self.assertEqual(self.store.fetch_batch(batch_id)["status"], "open")

        self.assertEqual(
            self.store.finish_programs("ver-1", ["solution-0"], now_text=_NOW),
            [batch_id],
        )
        terminal_claim = self.store.claim_batch_finalization(batch_id, now_text=_NOW)
        self.assertIsNotNone(terminal_claim)
        assert terminal_claim is not None
        self.assertTrue(terminal_claim.terminal_transition)
        self.assertIsNone(self.store.claim_batch_finalization(batch_id, now_text=_NOW))
        with self.assertRaisesRegex(RuntimeError, "verification program is closed"):
            _create_batch(
                self.store,
                task_id="task-too-late",
                run_id="run-too-late",
                case_rows=[_case_row("task-too-late", "run-too-late", "003.in", 1)],
                execution_signature="shared-signature",
                verification_program_id="solution-0",
            )
        self.assertEqual(len(self.store.cases_for_batch(batch_id)), 2)

    def test_publication_claim_retries_terminal_case_that_arrives_during_io(
        self,
    ) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-publication-race",
            run_id="run-publication-race",
            case_rows=[
                _case_row(
                    "task-publication-race",
                    "run-publication-race",
                    "001.in",
                    1,
                ),
                _case_row(
                    "task-publication-race",
                    "run-publication-race",
                    "002.in",
                    2,
                ),
            ],
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        first_case_id = int(
            next(
                row
                for row in self.store.cases_for_batch(batch_id)
                if row["test_name"] == "001.in"
            )["id"]
        )

        publication_claim = self.store.claim_batch_finalization(
            batch_id,
            now_text=_NOW,
        )
        self.assertIsNotNone(publication_claim)
        assert publication_claim is not None
        self.assertFalse(publication_claim.terminal_transition)

        # External publication owns an immutable snapshot. Simulate a second
        # callback becoming terminal before that I/O finishes.
        _finish_pending_case(self.store, batch_id, "002.in")
        second_case_id = int(
            next(
                row
                for row in self.store.cases_for_batch(batch_id)
                if row["test_name"] == "002.in"
            )["id"]
        )
        self.assertTrue(self.store.acknowledge_case_completion(first_case_id))
        self.assertTrue(self.store.complete_batch_finalization(publication_claim))

        self.assertEqual(
            self.store.due_batch_finalizations(limit=1),
            [batch_id],
        )
        next_claim = self.store.claim_batch_finalization(batch_id, now_text=_NOW)
        self.assertIsNotNone(next_claim)
        assert next_claim is not None
        self.assertFalse(next_claim.terminal_transition)
        self.assertFalse(
            next(
                row for row in next_claim.cases if int(row["id"]) == second_case_id
            )["completion_acknowledged"]
        )
        self.assertTrue(self.store.acknowledge_case_completion(second_case_id))
        self.assertTrue(self.store.complete_batch_finalization(next_claim))
        self.assertEqual(self.store.due_batch_finalizations(limit=1), [])

    def test_batch_identity_is_program_not_execution_signature(self) -> None:
        first_batch = _create_batch(
            self.store,
            task_id="task-program-first",
            run_id="run-program-first",
            case_rows=[
                _case_row("task-program-first", "run-program-first", "001.in", 1)
            ],
            execution_signature="shared-execution",
            verification_program_id="solution-0",
        )
        second_batch = _create_batch(
            self.store,
            task_id="task-program-second",
            run_id="run-program-second",
            case_rows=[
                _case_row("task-program-second", "run-program-second", "001.in", 1)
            ],
            execution_signature="shared-execution",
            verification_program_id="solution-1",
        )

        self.assertNotEqual(first_batch, second_batch)
        with self.assertRaisesRegex(
            RuntimeError,
            "verification program identity changed",
        ):
            _create_batch(
                self.store,
                task_id="task-program-first-later",
                run_id="run-program-first-later",
                case_rows=[
                    _case_row(
                        "task-program-first-later",
                        "run-program-first-later",
                        "002.in",
                        2,
                    )
                ],
                execution_signature="changed-execution",
                verification_program_id="solution-0",
            )

    def test_task_cases_cannot_span_batches(self) -> None:
        first_batch = _create_batch(
            self.store,
            task_id="task-first-batch",
            run_id="run-first-batch",
            case_rows=[_case_row("task-first-batch", "run-first-batch", "001.in", 1)],
        )
        second_batch = _create_batch(
            self.store,
            task_id="task-second-batch",
            run_id="run-second-batch",
            case_rows=[_case_row("task-second-batch", "run-second-batch", "001.in", 1)],
        )
        self.assertNotEqual(first_batch, second_batch)
        with self.assertRaisesRegex(RuntimeError, "already belong"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                case_rows=[
                    _case_row("task-first-batch", "run-first-batch", "002.in", 2)
                ],
                execution_signature="signature-task-second-batch",
                verification_program_id="solution-new",
            )
        duplicate = _create_batch(
            self.store,
            task_id="task-first-batch",
            run_id="run-first-batch",
            case_rows=[_case_row("task-first-batch", "run-first-batch", "001.in", 1)],
            execution_signature="signature-task-first-batch",
            verification_program_id="program-task-first-batch",
        )
        self.assertEqual(duplicate, first_batch)
        reordered = _case_row("task-first-batch", "run-first-batch", "001.in", 2)
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                case_rows=[reordered],
                execution_signature="signature-task-first-batch",
                verification_program_id="program-task-first-batch",
            )
        with self.assertRaisesRegex(RuntimeError, "case set is immutable"):
            _create_batch(
                self.store,
                task_id="task-first-batch",
                run_id="run-first-batch",
                case_rows=[
                    _case_row("task-first-batch", "run-first-batch", "001.in", 1),
                    _case_row("task-first-batch", "run-first-batch", "002.in", 2),
                ],
                execution_signature="signature-task-first-batch",
                verification_program_id="program-task-first-batch",
            )

    def test_forget_runs_removes_set_indexes(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-first",
            run_id="run-first",
            case_rows=[_case_row("task-first", "run-first", "001.in", 1)],
        )
        same_batch = _create_batch(
            self.store,
            task_id="task-second",
            run_id="run-second",
            case_rows=[_case_row("task-second", "run-second", "002.in", 1)],
            execution_signature="signature-task-first",
            verification_program_id="program-task-first",
        )
        self.assertEqual(same_batch, batch_id)

        removed_batches = self.store.forget_runs(["run-first", "run-second"])

        self.assertEqual(removed_batches, 1)
        self.assertIsNone(self.store.fetch_batch(batch_id))

    def test_quiet_cleanup_waits_for_callback_receipt_and_pending_diagnostic(
        self,
    ) -> None:
        case_spec = _case_row("task-pinned", "run-pinned", "001.in", 1)
        case_spec["verification_task_id"] = "verification-task-pinned"
        batch_id = _create_batch(
            self.store,
            task_id="task-pinned",
            run_id="run-pinned",
            case_rows=[case_spec],
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        case = self.store.cases_for_batch(batch_id)[0]
        case_id = int(case["id"])
        self.assertTrue(self.store.acknowledge_case_completion(case_id))

        receipt = self.store.acquire_case_callback_receipt(case_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertIsNone(self.store.forget_runs_if_quiet(["run-pinned"]))
        self.store.release_case_callback_receipt(receipt.receipt_id)

        disposition = self.store.record_case_diagnostic(
            case_id,
            kind="debug-info",
            hostname="host-001.in",
            text="late compare diagnostic",
            receipt_generation=receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            now_text=_NOW,
        )
        self.assertEqual(disposition, "pending")
        self.assertIsNone(self.store.forget_runs_if_quiet(["run-pinned"]))
        diagnostic = self.store.pending_case_diagnostics(case_id)[0]
        self.assertEqual(diagnostic.received_at, _NOW)
        self.assertTrue(self.store.acknowledge_case_diagnostic(case_id, diagnostic))

        self.assertEqual(self.store.forget_runs_if_quiet(["run-pinned"]), 1)
        self.assertIsNone(self.store.fetch_case(case_id))

    def test_quiet_cleanup_does_not_remove_active_finalization_claim(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-finalizing",
            run_id="run-finalizing",
            case_rows=[_case_row("task-finalizing", "run-finalizing", "001.in", 1)],
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        case_id = int(self.store.cases_for_batch(batch_id)[0]["id"])
        self.assertTrue(self.store.acknowledge_case_completion(case_id))
        self.store.finish_verification_execution("ver-1", now_text=_NOW)
        claim = self.store.claim_batch_finalization(batch_id, now_text=_NOW)
        self.assertIsNotNone(claim)

        self.assertIsNone(self.store.forget_runs_if_quiet(["run-finalizing"]))
        self.assertIsNotNone(self.store.fetch_batch(batch_id))

    def test_stale_callback_receipt_cannot_write_into_a_new_same_host_lease(
        self,
    ) -> None:
        case_spec = _case_row("task-stale", "run-stale", "001.in", 1)
        case_spec["verification_task_id"] = "verification-task-stale"
        batch_id = _create_batch(
            self.store,
            task_id="task-stale",
            run_id="run-stale",
            case_rows=[case_spec],
        )
        first_lease = _lease_cases(
            self.store,
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=_NOW,
        )[0]
        case_id = int(first_lease["id"])
        stale_receipt = self.store.acquire_case_callback_receipt(case_id)
        assert stale_receipt is not None

        self.store.release_host_leases("host-a", now_text=_NOW)
        second_lease = _lease_cases(
            self.store,
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=_NOW,
        )[0]
        self.assertEqual(int(second_lease["id"]), case_id)

        self.assertIsNone(
            self.store.claim_case_reporting(
                case_id,
                hostname="host-a",
                receipt_generation=stale_receipt.claim_generation,
                now_text=_NOW,
            )
        )
        self.assertFalse(
            self.store.record_compile_success(
                case_id,
                hostname="host-a",
                receipt_generation=stale_receipt.claim_generation,
                compile_output_b64="",
                compile_metadata_b64="",
                updated_at=_NOW,
            )
        )
        compile_failure = self.store.claim_compile_failure(
            case_id,
            hostname="host-a",
            receipt_generation=stale_receipt.claim_generation,
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="stale compile failure",
            compile_log="stale compile failure",
            compile_diagnostics=(),
            updated_at=_NOW,
        )
        self.assertEqual(compile_failure.outcome, "rejected")
        internal_error = self.store.claim_internal_error(
            case_id,
            hostname="host-a",
            failure_text="stale internal error",
            diagnostic_text="stale internal error",
            receipt_generation=stale_receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            updated_at=_NOW,
        )
        self.assertEqual(internal_error.outcome, "rejected")

        disposition = self.store.record_case_diagnostic(
            case_id,
            kind="debug-info",
            hostname="host-a",
            text="stale callback must not survive re-lease",
            receipt_generation=stale_receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            now_text=_NOW,
        )
        self.store.release_case_callback_receipt(stale_receipt.receipt_id)

        self.assertEqual(disposition, "rejected")
        self.assertEqual(self.store.pending_case_diagnostics(case_id), ())
        debug = self.store.case_debug_context(case_id)
        assert debug is not None
        self.assertEqual(debug["case_debug_text"], "")

    def test_pending_diagnostics_dedupe_and_evict_within_serialized_limit(self) -> None:
        case_spec = _case_row("task-diagnostics", "run-diagnostics", "001.in", 1)
        case_spec["verification_task_id"] = "verification-task-diagnostics"
        batch_id = _create_batch(
            self.store,
            task_id="task-diagnostics",
            run_id="run-diagnostics",
            case_rows=[case_spec],
        )
        _finish_pending_case(self.store, batch_id, "001.in")
        case_id = int(self.store.cases_for_batch(batch_id)[0]["id"])
        self.assertTrue(self.store.acknowledge_case_completion(case_id))
        receipt = self.store.acquire_case_callback_receipt(case_id)
        assert receipt is not None

        for received_at in (
            "2026-07-29T00:00:01+00:00",
            "2026-07-29T00:00:02+00:00",
        ):
            self.assertEqual(
                self.store.record_case_diagnostic(
                    case_id,
                    kind="debug-info",
                    hostname="host-001.in",
                    text='same "quoted" diagnostic\\path',
                    receipt_generation=receipt.claim_generation,
                    diagnostic_limit_bytes=512,
                    now_text=received_at,
                ),
                "pending",
            )
        self.assertEqual(len(self.store.pending_case_diagnostics(case_id)), 1)

        for index in range(48):
            self.store.record_case_diagnostic(
                case_id,
                kind="debug-info",
                hostname="host-001.in",
                text=f"diagnostic-{index}-" + ("x" * 96),
                receipt_generation=receipt.claim_generation,
                diagnostic_limit_bytes=512,
                now_text=f"2026-07-29T00:01:{index:02d}+00:00",
            )
        diagnostics = self.store.pending_case_diagnostics(case_id)
        self.store.release_case_callback_receipt(receipt.receipt_id)
        serialized = json.dumps(
            {
                "items": [
                    {
                        "kind": item.kind,
                        "hostname": item.hostname,
                        "text": item.text,
                        "received_at": item.received_at,
                        "digest": item.digest,
                    }
                    for item in diagnostics
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertLessEqual(len(diagnostics), 32)
        self.assertLessEqual(len(serialized), 512)
        self.assertTrue(diagnostics[-1].text.startswith("diagnostic-47-"))
        self.assertNotIn(
            'same "quoted" diagnostic\\path',
            [item.text for item in diagnostics],
        )

    def test_verification_index_cancels_multiple_batches_without_history_scan(
        self,
    ) -> None:
        batch_ids = []
        for sequence in range(2):
            task_id = f"task-shared-run-{sequence}"
            run_id = f"primary-run-{sequence}"
            batch_ids.append(
                _create_batch(
                    self.store,
                    task_id=task_id,
                    run_id=run_id,
                    case_rows=[_case_row(task_id, run_id, "001.in", 1)],
                )
            )

        cancellation = self.store.request_verification_cancel("ver-1", now_text=_NOW)
        self.assertEqual(list(cancellation.batch_ids), batch_ids)
        self.assertEqual(
            [
                self.store.cases_for_batch(batch_id)[0]["status"]
                for batch_id in batch_ids
            ],
            ["cancelled", "cancelled"],
        )

    def test_rows_keep_public_shapes_and_progress_uses_incremental_counts(self) -> None:
        batch_id = _create_batch(
            self.store,
            task_id="task-shapes",
            run_id="run-shapes",
            case_rows=[
                _case_row("task-shapes", "run-shapes", "001.in", 1),
                _case_row("task-shapes", "run-shapes", "002.in", 2),
            ],
        )
        batch = self.store.fetch_batch(batch_id)
        cases = _lease_cases(
            self.store, batch_id, hostname="host-a", limit=1, now_text=_NOW
        )

        self.assertIsNotNone(batch)
        self.assertEqual(set(batch), set(ExecutionBatchRow.__annotations__))
        self.assertEqual(set(cases[0]), set(JudgehostCaseRow.__annotations__))
        self.assertEqual(
            self.store.case_progress_for_runs(["run-shapes"]),
            {"run-shapes": {"total": 2, "reported": 0, "leased": 1}},
        )


class TestWriterPriorityRWLock(unittest.TestCase):
    def test_waiting_writer_blocks_new_readers(self) -> None:
        lock = WriterPriorityRWLock()
        first_reader_entered = threading.Event()
        release_first_reader = threading.Event()
        writer_waiting = threading.Event()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        second_reader_entered = threading.Event()
        order: list[str] = []

        def first_reader() -> None:
            with lock.read_lock():
                first_reader_entered.set()
                release_first_reader.wait(timeout=2)

        def writer() -> None:
            writer_waiting.set()
            with lock.write_lock():
                order.append("writer")
                writer_entered.set()
                release_writer.wait(timeout=2)

        def second_reader() -> None:
            with lock.read_lock():
                order.append("reader")
                second_reader_entered.set()

        threads = [
            threading.Thread(target=first_reader),
            threading.Thread(target=writer),
            threading.Thread(target=second_reader),
        ]
        threads[0].start()
        self.assertTrue(first_reader_entered.wait(timeout=2))
        threads[1].start()
        self.assertTrue(writer_waiting.wait(timeout=2))
        time.sleep(0.01)
        threads[2].start()
        self.assertFalse(second_reader_entered.wait(timeout=0.05))
        release_first_reader.set()
        self.assertTrue(writer_entered.wait(timeout=2))
        self.assertFalse(second_reader_entered.wait(timeout=0.05))
        release_writer.set()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(order, ["writer", "reader"])

    def test_lock_rejects_recursive_acquisition(self) -> None:
        lock = WriterPriorityRWLock()
        with lock.read_lock():
            with self.assertRaisesRegex(RuntimeError, "non-reentrant"):
                with lock.write_lock():
                    pass
