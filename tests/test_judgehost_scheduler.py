import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import build_config_values
from app.service.judgehost.work.cleanup import JudgehostTerminalCleanup
from app.service.judgehost.callback.case_result import build_case_result
from app.service.judgehost.domjudge.identity import script_id
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.batch.model import (
    CaseClaimBusy,
    CaseResult,
    CompileSubmission,
    ExecutionBatchSpec,
)
from app.service.judgehost.domjudge.identity import submit_id
from app.service.judgehost.work.task_registry import JudgehostTaskRegistry
from app.service.judgehost.domjudge.toolkit import DomjudgeToolkit
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


def _case_result(test_name: str, *, runresult: str = "correct", verdict: str = "OK"):
    artifact_ref = "blob://sha256/" + hashlib.sha256(test_name.encode("utf-8")).hexdigest()
    return build_case_result(
        test_name=test_name,
        runresult=runresult,
        verdict=verdict,
        runtime_sec=0.001,
        cpu_sec=0.001,
        wall_sec=0.002,
        memory_kb=1024,
        score_text="",
        output_run_ref=artifact_ref,
        output_error_ref=artifact_ref,
        output_system_ref=artifact_ref,
        output_diff_ref=artifact_ref,
        metadata_ref=artifact_ref,
        compare_metadata_ref=artifact_ref,
        team_message_ref=artifact_ref,
        feedback_text="",
        feedback_files=[],
        answer_correct=False,
        input_ref=artifact_ref,
    )


def _receipt_generation(scheduler: JudgehostBatchRuntime, case_id: int) -> int:
    receipt = scheduler.acquire_case_callback_receipt(case_id)
    if receipt is None:
        raise AssertionError(f"case {case_id} has no callback identity")
    scheduler.release_case_callback_receipt(receipt.receipt_id)
    return receipt.claim_generation


def _commit_leased_case_result(
    scheduler: JudgehostBatchRuntime,
    *,
    case_id: int,
    hostname: str,
    result: CaseResult,
    updated_at: str,
) -> str:
    claim = scheduler.claim_case_reporting(
        case_id,
        hostname=hostname,
        receipt_generation=_receipt_generation(scheduler, case_id),
        now_text=updated_at,
    )
    if claim is None:
        raise AssertionError(f"case {case_id} could not enter reporting")
    return scheduler.commit_case_result(
        case_id,
        generation=claim.generation,
        result=result,
        updated_at=updated_at,
    )


_COMPILE_KEY = "5" * 64


def _submission_for_key(compile_key: str) -> CompileSubmission:
    return CompileSubmission(
        compile_key=compile_key,
        submit_id=submit_id(compile_key),
        source_name="main.cpp",
        source_file=PayloadFile(
            path=Path("/tmp/test-main.cpp"),
            size=len(compile_key),
            identity=hashlib.sha256(compile_key.encode("ascii")).hexdigest(),
        ),
        extra_source_items=(),
        compile_files=(),
    )


def _task_row(index: int, *, verification_id: str = "ver-1") -> dict[str, object]:
    now_text = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"task-{index}",
        "run_id": f"run-{index}",
        "problem_slug": "owner/problem",
        "username": "owner",
        "artifact_verification_id": verification_id,
        "mode": "pass-fail",
        "verification_id": verification_id,
        "status": "queued",
        "payload": {},
        "result": {},
        "persist_verification_run": False,
        "error_text": "",
        "created_at": now_text,
        "updated_at": now_text,
        "completed_at": "",
        "summary": {},
        "enqueue_fingerprint": f"fingerprint-{index}",
    }


def _case_row(task_id: str, run_id: str, ordinal: int, scope: int) -> dict[str, object]:
    token = f"{ordinal:064x}"
    return {
        "task_id": task_id,
        "verification_task_id": task_id,
        "run_id": run_id,
        "test_name": f"{ordinal:03}.in",
        "ordinal": ordinal,
        "scope_sequence": scope,
        "testcase_id": ordinal,
        "testcase_hash": token,
        "testcase_input_hash": token,
        "testcase_answer_hash": token,
        "input_ref": f"blob://sha256/{token}",
        "answer_ref": f"blob://sha256/{token}",
        "status": "staged",
    }


def _create_staged_batch(
    scheduler: JudgehostBatchRuntime,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
    case_rows: list[dict[str, object]] | None = None,
    verification_id: str = "ver-1",
    compile_key: str = _COMPILE_KEY,
    compile_hash: str = "1" * 32,
    execution_signature: str | None = None,
    verification_program_id: str | None = None,
    task_kind: str = "solution-run",
) -> tuple[int, str]:
    now_text = datetime.now(timezone.utc).isoformat()
    signature = hashlib.sha256(
        (execution_signature or f"signature-{task_id}").encode("utf-8")
    ).hexdigest()
    batch_id = scheduler.create_batch_with_cases(
        task_id=task_id,
        run_id=run_id,
        verification_program_id=(
            task_id if verification_program_id is None else verification_program_id
        ),
        execution_signature=signature,
        task_kind=task_kind,
        verification_id=verification_id,
        compile_key=compile_key,
        compile_submission=_submission_for_key(compile_key),
        contest_id="local",
        mode="pass-fail",
        source_name="main.cpp",
        compile_hash=compile_hash,
        run_hash="2" * 32,
        compare_hash="3" * 32,
        source_hash="4" * 64,
        compile_config_json="{}",
        run_config_json="{}",
        compare_config_json="{}",
        expected_behavior="accepted",
        verification_source="solution-run",
        bypass_case_result_cache=0,
        service_class=service_class,
        batch_spec=ExecutionBatchSpec(),
        created_at=now_text,
        case_rows=(
            [_case_row(task_id, run_id, ordinal, scope) for ordinal in ordinals]
            if case_rows is None
            else case_rows
        ),
    )
    return batch_id, now_text


def _create_ready_batch(
    scheduler: JudgehostBatchRuntime,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
    verification_id: str = "ver-1",
    task_kind: str = "solution-run",
    case_rows: list[dict[str, object]] | None = None,
    execution_signature: str | None = None,
    verification_program_id: str | None = None,
) -> int:
    batch_id, now_text = _create_staged_batch(
        scheduler,
        task_id=task_id,
        run_id=run_id,
        ordinals=ordinals,
        service_class=service_class,
        scope=scope,
        verification_id=verification_id,
        task_kind=task_kind,
        case_rows=case_rows,
        execution_signature=execution_signature,
        verification_program_id=verification_program_id,
    )
    scheduler.activate_task_cases(task_id, now_text=now_text)
    claims = scheduler.claim_cache_cases(
        batch_id,
        hostname="cache-setup",
        limit=len(ordinals),
        now_text=now_text,
    )
    for claim, _row in claims:
        scheduler.finish_cache_miss(
            claim.case_id,
            generation=claim.generation,
            updated_at=now_text,
        )
    scheduler.claim_materialization(batch_id, now_text=now_text)
    scheduler.finish_materialization(
        batch_id,
        success=True,
        error_text="",
        now_text=now_text,
    )
    setup_hostname = f"compile-setup-{batch_id}"
    setup_case = scheduler.lease_cases(
        batch_id,
        hostname=setup_hostname,
        limit=1,
        now_text=now_text,
    )[0]
    assert scheduler.record_compile_success(
        int(setup_case["id"]),
        hostname=setup_hostname,
        receipt_generation=_receipt_generation(
            scheduler,
            int(setup_case["id"]),
        ),
        compile_output_b64="",
        compile_metadata_b64="",
        updated_at=now_text,
    )
    release = scheduler.release_host_leases(
        setup_hostname,
        now_text=now_text,
    )
    assert release.lease_count == 1
    return batch_id


class TestJudgehostScheduler(unittest.TestCase):
    @staticmethod
    def _runtime_toolkit(
        temp_root: Path,
    ) -> tuple[DomjudgeToolkit, RuntimeBlobStore, RuntimeCacheIndex]:
        blob_store = RuntimeBlobStore(temp_root / "blobs")
        cache_index = RuntimeCacheIndex(blob_store)
        state = SimpleNamespace(
            runtime_blob_store=blob_store,
            runtime_cache_index=cache_index,
        )
        return (DomjudgeToolkit(state), blob_store, cache_index)

    def test_default_entity_ids_use_nanosecond_base_and_survive_reset(self) -> None:
        with patch(
            "app.service.judgehost.batch.state.time.time_ns",
            return_value=1_000_000,
        ):
            scheduler = JudgehostBatchRuntime()
        first_batch, _now_text = _create_staged_batch(
            scheduler,
            task_id="nanosecond-first",
            run_id="run-nanosecond-first",
            ordinals=[1],
        )
        first_case = scheduler.cases_for_batch(first_batch)[0]
        self.assertEqual((first_batch, int(first_case["id"])), (1_000_001, 1_000_002))

        scheduler.reset()
        second_batch, _now_text = _create_staged_batch(
            scheduler,
            task_id="nanosecond-second",
            run_id="run-nanosecond-second",
            ordinals=[1],
        )
        second_case = scheduler.cases_for_batch(second_batch)[0]
        self.assertEqual((second_batch, int(second_case["id"])), (1_000_003, 1_000_004))

    def test_entity_id_reservation_rejects_overflow_without_partial_state(self) -> None:
        max_id = (1 << 63) - 1
        scheduler = JudgehostBatchRuntime(id_base=max_id - 2)
        batch_id, _now_text = _create_staged_batch(
            scheduler,
            task_id="boundary-first",
            run_id="run-boundary-first",
            ordinals=[1],
            execution_signature="boundary-signature",
            verification_program_id="solution-0",
        )
        self.assertEqual(batch_id, max_id - 1)
        self.assertEqual(int(scheduler.cases_for_batch(batch_id)[0]["id"]), max_id)

        with self.assertRaisesRegex(OverflowError, "signed 64-bit"):
            _create_staged_batch(
                scheduler,
                task_id="boundary-overflow",
                run_id="run-boundary-overflow",
                ordinals=[2],
                execution_signature="boundary-signature",
                verification_program_id="solution-0",
            )
        self.assertIsNone(scheduler.batch_for_task("boundary-overflow"))
        self.assertEqual(len(scheduler.cases_for_batch(batch_id)), 1)

    def test_nanosecond_restart_bases_do_not_overlap_at_supported_creation_rate(self) -> None:
        with patch(
            "app.service.judgehost.batch.state.time.time_ns",
            side_effect=[2_000_000, 2_000_100],
        ):
            old_scheduler = JudgehostBatchRuntime()
            new_scheduler = JudgehostBatchRuntime()
        old_batch, _now_text = _create_staged_batch(
            old_scheduler,
            task_id="old-process",
            run_id="run-old-process",
            ordinals=[1, 2, 3],
        )
        new_batch, _now_text = _create_staged_batch(
            new_scheduler,
            task_id="new-process",
            run_id="run-new-process",
            ordinals=[1],
        )
        old_ids = {old_batch, *(int(row["id"]) for row in old_scheduler.cases_for_batch(old_batch))}
        new_ids = {new_batch, *(int(row["id"]) for row in new_scheduler.cases_for_batch(new_batch))}
        self.assertTrue(old_ids.isdisjoint(new_ids))

    def test_materialized_compile_source_outlives_snapshot_descriptor(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=100)
        _create_staged_batch(
            scheduler,
            task_id="task-source-a",
            run_id="run-source-a",
            ordinals=[1],
            verification_id="ver-1",
            compile_key=_COMPILE_KEY,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_source = root / "snapshot" / "main.cpp"
            snapshot_source.parent.mkdir(parents=True)
            snapshot_source.write_bytes(_COMPILE_KEY.encode("ascii"))
            blob_store = RuntimeBlobStore(root / "runtime" / "blobs")
            stored_source = blob_store.put_file(RuntimeBlobStore.describe_file(snapshot_source))
            materialized = CompileSubmission(
                compile_key=_COMPILE_KEY,
                submit_id=submit_id(_COMPILE_KEY),
                source_name="main.cpp",
                source_file=stored_source,
                extra_source_items=(),
                compile_files=(),
            )
            scheduler.publish_materialized_compile_submission(_COMPILE_KEY, materialized)
            snapshot_source.unlink()

            source = scheduler.source_submission(str(materialized.submit_id), contest_id="local")
            self.assertIsNotNone(source)
            assert source is not None
            self.assertEqual(source.source_file.path.read_bytes(), _COMPILE_KEY.encode("ascii"))

            # A later Verification may describe the same source from another snapshot.
            _create_staged_batch(
                scheduler,
                task_id="task-source-b",
                run_id="run-source-b",
                ordinals=[2],
                verification_id="ver-2",
                compile_key=_COMPILE_KEY,
            )

    def test_materialized_submission_remains_canonical_when_program_appends(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=100)
        batch_id, _now_text = _create_staged_batch(
            scheduler,
            task_id="task-warm-a",
            run_id="run-warm-a",
            ordinals=[1],
            verification_program_id="solution-0",
            execution_signature="warm-execution",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "main.cpp"
            source.write_bytes(_COMPILE_KEY.encode("ascii"))
            blob_store = RuntimeBlobStore(root / "runtime" / "blobs")
            materialized = CompileSubmission(
                compile_key=_COMPILE_KEY,
                submit_id=submit_id(_COMPILE_KEY),
                source_name="main.cpp",
                source_file=blob_store.put_file(source),
                extra_source_items=(),
                compile_files=(),
            )
            scheduler.publish_materialized_compile_submission(_COMPILE_KEY, materialized)

            appended_batch_id, _ = _create_staged_batch(
                scheduler,
                task_id="task-warm-b",
                run_id="run-warm-b",
                ordinals=[2],
                verification_program_id="solution-0",
                execution_signature="warm-execution",
            )

        self.assertEqual(appended_batch_id, batch_id)
        self.assertEqual(
            scheduler.compile_submission_for_batch(batch_id),
            materialized,
        )

    def test_task_registry_has_identity_but_no_scheduler(self) -> None:
        registry = JudgehostTaskRegistry()
        registry.insert(_task_row(1))
        self.assertEqual(registry.get_for_run("run-1")["id"], "task-1")
        self.assertFalse(hasattr(registry, "claim_ready"))
        self.assertFalse(hasattr(registry, "renew"))

    def test_invalid_case_spec_does_not_partially_create_batch(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=50)
        invalid_case = _case_row("invalid", "run-invalid", 1, 1)
        invalid_case.pop("answer_ref")
        with self.assertRaisesRegex(RuntimeError, "missing answer_ref"):
            _create_staged_batch(
                scheduler,
                task_id="invalid",
                run_id="run-invalid",
                ordinals=[1],
                case_rows=[invalid_case],
            )
        self.assertIsNone(scheduler.fetch_batch(51))

        with self.assertRaisesRegex(RuntimeError, "invalid script hash"):
            _create_staged_batch(
                scheduler,
                task_id="invalid-script",
                run_id="run-invalid-script",
                ordinals=[1],
                compile_hash="not-a-script-hash",
            )
        self.assertIsNone(scheduler.fetch_batch(51))

    def test_ready_batch_prefers_foreground_then_numeric_test_order(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=100)
        background_id = _create_ready_batch(
            scheduler,
            task_id="background",
            run_id="run-background",
            ordinals=[10, 2, 1],
        )
        foreground_id = _create_ready_batch(
            scheduler,
            task_id="foreground",
            run_id="run-foreground",
            ordinals=[3],
            service_class="foreground",
            scope=2,
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], foreground_id)
        self.assertEqual(scheduler.batch_dispatch_count(foreground_id), 1)
        foreground = scheduler.lease_cases(
            foreground_id,
            hostname="host-a",
            limit=2,
            now_text=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual([row["ordinal"] for row in foreground], [3])
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], background_id)
        background = scheduler.lease_cases(
            background_id,
            hostname="host-b",
            limit=3,
            now_text=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual([row["ordinal"] for row in background], [1, 2, 10])

    def test_ready_batch_spreads_first_claims_within_verification(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=125)
        first_id = _create_ready_batch(
            scheduler,
            task_id="spread-first",
            run_id="run-spread-first",
            ordinals=[1],
        )
        second_id = _create_ready_batch(
            scheduler,
            task_id="spread-second",
            run_id="run-spread-second",
            ordinals=[2],
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], second_id)
        self.assertEqual(scheduler.select_ready_batch("host-c")["batch_id"], first_id)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)

    def test_global_ready_order_uses_dispatch_scope_pending_and_id(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=128)
        smaller_id = _create_ready_batch(
            scheduler,
            task_id="global-small",
            run_id="run-global-small",
            ordinals=[1],
        )
        larger_id = _create_ready_batch(
            scheduler,
            task_id="global-large",
            run_id="run-global-large",
            ordinals=[2, 3, 4],
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], larger_id)
        self.assertEqual(scheduler.batch_dispatch_count(larger_id), 1)
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], smaller_id)
        self.assertEqual(scheduler.batch_dispatch_count(smaller_id), 1)

    def test_existing_lease_and_affinity_reuse_do_not_increment_dispatch(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=130)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="dispatch-reuse",
            run_id="run-dispatch-reuse",
            ordinals=[1, 2],
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
        self.assertEqual(scheduler.batch_dispatch_count(batch_id), 1)
        leased = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )[0]

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
        self.assertEqual(scheduler.batch_dispatch_count(batch_id), 1)
        self.assertEqual(
            _commit_leased_case_result(
                scheduler,
                case_id=int(leased["id"]),
                hostname="host-a",
                result=_case_result("001.in"),
                updated_at=datetime.now(timezone.utc).isoformat(),
            ),
            "reported",
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
        self.assertEqual(scheduler.batch_dispatch_count(batch_id), 1)

    def test_host_queue_keeps_temporarily_blocked_batch(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=132)
        first_id = _create_ready_batch(
            scheduler,
            task_id="affinity-first",
            run_id="run-affinity-first",
            ordinals=[1],
            verification_program_id="solution-0",
        )
        second_id = _create_ready_batch(
            scheduler,
            task_id="affinity-help",
            run_id="run-affinity-help",
            ordinals=[2],
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)
        first_case = scheduler.lease_cases(
            first_id,
            hostname="host-a",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )[0]
        self.assertEqual(
            _commit_leased_case_result(
                scheduler,
                case_id=int(first_case["id"]),
                hostname="host-a",
                result=_case_result("001.in"),
                updated_at=datetime.now(timezone.utc).isoformat(),
            ),
            "reported",
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], second_id)
        self.assertEqual(
            [row["batch_id"] for row in scheduler.host_context_batches("host-a")],
            [first_id, second_id],
        )

        first_again, now_text = _create_staged_batch(
            scheduler,
            task_id="affinity-first-later",
            run_id="run-affinity-first-later",
            ordinals=[3],
            execution_signature="signature-affinity-first",
            verification_program_id="solution-0",
        )
        self.assertEqual(first_again, first_id)
        scheduler.activate_task_cases("affinity-first-later", now_text=now_text)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)

    def test_host_release_clears_local_queue_without_resetting_dispatch_order(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=138)
        first_id = _create_ready_batch(
            scheduler,
            task_id="released-affinity",
            run_id="run-released-affinity",
            ordinals=[1, 2],
        )
        other_id = _create_ready_batch(
            scheduler,
            task_id="other-affinity",
            run_id="run-other-affinity",
            ordinals=[2],
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)
        leased = scheduler.lease_cases(
            first_id,
            hostname="host-a",
            limit=2,
            now_text=datetime.now(timezone.utc).isoformat(),
        )
        dispatch_count = scheduler.batch_dispatch_count(first_id)
        batch = scheduler.fetch_batch(first_id)
        assert batch is not None

        release = scheduler.release_host_leases(
            "host-a",
            now_text=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual((release.affinity_count, release.lease_count), (1, 2))
        self.assertEqual(
            release.workdirs,
            ((int(batch["job_id"]), submit_id(_COMPILE_KEY)),),
        )
        self.assertEqual(scheduler.batch_dispatch_count(first_id), dispatch_count)
        self.assertEqual(scheduler.batch_case_count(first_id, status="pending"), 2)
        for row in leased:
            released = scheduler.fetch_case(int(row["id"]))
            assert released is not None
            self.assertEqual((released["status"], released["lease_owner"]), ("pending", None))
        self.assertEqual(scheduler.host_context_batches("host-a"), [])
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], other_id)
        self.assertEqual(scheduler.select_ready_batch("host-c")["batch_id"], first_id)
        reassigned = scheduler.lease_cases(
            first_id,
            hostname="host-c",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )[0]
        self.assertIsNone(
            scheduler.claim_case_reporting(
                int(reassigned["id"]),
                hostname="host-a",
                receipt_generation=_receipt_generation(
                    scheduler,
                    int(reassigned["id"]),
                ),
                now_text=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.assertEqual(
            scheduler.release_host_leases(
                "host-a",
                now_text=datetime.now(timezone.utc).isoformat(),
            ).workdirs,
            (),
        )

    def test_host_release_defers_reporting_until_claim_finishes(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=142)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="released-reporting",
            run_id="run-released-reporting",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-05T00:00:00+00:00",
        )[0]
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            now_text="2026-08-05T00:00:01+00:00",
        )
        assert claim is not None

        first_release = scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-05T00:00:02+00:00",
        )

        self.assertEqual(first_release.lease_count, 1)
        self.assertEqual(scheduler.fetch_case(claim.case_id)["status"], "reporting")
        self.assertEqual(
            scheduler.release_host_leases(
                "host-a",
                now_text="2026-08-05T00:00:03+00:00",
            ).workdirs,
            (),
        )
        self.assertTrue(
            scheduler.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at="2026-08-05T00:00:04+00:00",
            )
        )
        self.assertEqual(scheduler.fetch_case(claim.case_id)["status"], "pending")

    def test_host_release_keeps_successful_reporting_commit_valid(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=144)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="released-reporting-success",
            run_id="run-released-reporting-success",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-05T00:00:00+00:00",
        )[0]
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            now_text="2026-08-05T00:00:01+00:00",
        )
        assert claim is not None
        scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-05T00:00:02+00:00",
        )

        self.assertEqual(
            scheduler.commit_case_result(
                claim.case_id,
                generation=claim.generation,
                result=_case_result("001.in"),
                updated_at="2026-08-05T00:00:03+00:00",
            ),
            "reported",
        )
        self.assertEqual(scheduler.fetch_case(claim.case_id)["status"], "reported")

    def test_host_release_finishes_cancelled_lease_without_requeue(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=146)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="released-cancelled",
            run_id="run-released-cancelled",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-05T00:00:00+00:00",
        )[0]
        scheduler.request_verification_cancel(
            "ver-1",
            now_text="2026-08-05T00:00:01+00:00",
        )

        release = scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-05T00:00:02+00:00",
        )

        self.assertEqual(release.terminal_task_ids, ("released-cancelled",))
        self.assertEqual(scheduler.fetch_case(int(case["id"]))["status"], "cancelled")
        self.assertEqual(scheduler.batch_case_count(batch_id, status="pending"), 0)
        self.assertEqual(
            scheduler.lease_cases(batch_id, hostname="host-b", limit=1, now_text="later"), []
        )

    def test_host_release_cancelled_reporting_aborts_to_cancelled(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=148)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="released-reporting-cancelled",
            run_id="run-released-reporting-cancelled",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-05T00:00:00+00:00",
        )[0]
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            now_text="2026-08-05T00:00:01+00:00",
        )
        assert claim is not None
        scheduler.request_verification_cancel(
            "ver-1",
            now_text="2026-08-05T00:00:02+00:00",
        )
        scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-05T00:00:03+00:00",
        )

        self.assertTrue(
            scheduler.abort_case_claim(
                claim.case_id,
                generation=claim.generation,
                updated_at="2026-08-05T00:00:04+00:00",
            )
        )
        self.assertEqual(scheduler.fetch_case(claim.case_id)["status"], "cancelled")
        self.assertTrue(scheduler.task_cases_terminal("released-reporting-cancelled"))

    def test_contradictory_compile_failure_stays_on_unique_execution_batch(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=144)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="failed-first",
            run_id="run-failed-first",
            ordinals=[1],
            execution_signature="sticky-failure",
            verification_program_id="solution-0",
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        compile_failure = scheduler.claim_compile_failure(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="compilation failed",
            compile_log="compilation failed",
            compile_diagnostics=(),
            updated_at="2026-08-11T00:00:01+00:00",
        )
        self.assertEqual(compile_failure.outcome, "claimed")

        same_batch_id, _ = _create_staged_batch(
            scheduler,
            task_id="failed-later",
            run_id="run-failed-later",
            ordinals=[2],
            execution_signature="sticky-failure",
            verification_program_id="solution-0",
        )

        self.assertEqual(same_batch_id, batch_id)
        self.assertEqual(scheduler.fetch_batch(batch_id)["failure_runresult"], "internal-error")
        self.assertEqual(len(scheduler.cases_for_batch(batch_id)), 2)
        self.assertTrue(
            scheduler.record_compile_success(
                int(case["id"]),
                hostname="host-a",
                receipt_generation=_receipt_generation(
                    scheduler,
                    int(case["id"]),
                ),
                compile_output_b64="",
                compile_metadata_b64="",
                updated_at="2026-08-11T00:00:02+00:00",
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "succeeded")

    def test_final_run_claim_prevents_late_compile_failure_overwrite(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=147)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="final-before-compile-failure",
            run_id="run-final-before-compile-failure",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        lease_generation = _receipt_generation(scheduler, int(case["id"]))
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=lease_generation,
            now_text="2026-08-11T00:00:01+00:00",
        )
        assert claim is not None

        compile_failure = scheduler.claim_compile_failure(
            claim.case_id,
            hostname="host-a",
            receipt_generation=lease_generation,
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="late compiler failure",
            compile_log="late compiler failure",
            compile_diagnostics=(),
            updated_at="2026-08-11T00:00:02+00:00",
        )
        committed = scheduler.commit_case_result(
            claim.case_id,
            generation=claim.generation,
            result=_case_result("001.in"),
            updated_at="2026-08-11T00:00:03+00:00",
        )

        self.assertEqual(compile_failure.outcome, "late")
        self.assertEqual(committed, "reported")
        persisted = scheduler.fetch_case(claim.case_id)
        assert persisted is not None
        self.assertEqual(str(persisted["runresult"]), "correct")
        batch = scheduler.fetch_batch(batch_id)
        assert batch is not None
        self.assertEqual(str(batch["compile_state"]), "succeeded")
        self.assertEqual(str(batch["failure_runresult"]), "")

    def test_cancelled_lease_wins_over_contradictory_compile_failure(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=148)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="cancel-before-compile-failure",
            run_id="run-cancel-before-compile-failure",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        scheduler.request_verification_cancel(
            "ver-1",
            now_text="2026-08-11T00:00:01+00:00",
        )

        claim = scheduler.claim_compile_failure(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="contradictory compiler failure",
            compile_log="contradictory compiler failure",
            compile_diagnostics=(),
            updated_at="2026-08-11T00:00:02+00:00",
        )

        self.assertEqual(claim.outcome, "cancelled")
        self.assertEqual(scheduler.fetch_case(int(case["id"]))["status"], "cancelled")
        self.assertEqual(scheduler.fetch_batch(batch_id)["failure_runresult"], "")

    def test_internal_error_primary_retry_is_idempotent(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=149)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="internal-error-retry",
            run_id="run-internal-error-retry",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        receipt = scheduler.acquire_case_callback_receipt(int(case["id"]))
        assert receipt is not None

        first = scheduler.claim_internal_error(
            int(case["id"]),
            hostname="host-a",
            failure_text="judgehost crashed\n\ntrace one",
            diagnostic_text="judgehost crashed\n\ntrace one",
            receipt_generation=receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            updated_at="2026-08-11T00:00:01+00:00",
        )
        retry = scheduler.claim_internal_error(
            int(case["id"]),
            hostname="host-a",
            failure_text="judgehost crashed\n\ntrace one",
            diagnostic_text="judgehost crashed\n\ntrace one",
            receipt_generation=receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            updated_at="2026-08-11T00:00:02+00:00",
        )

        self.assertEqual(first.outcome, "claimed")
        self.assertEqual(retry.outcome, "idempotent")
        self.assertEqual(scheduler.pending_case_diagnostics(int(case["id"])), ())
        distinct = scheduler.claim_internal_error(
            int(case["id"]),
            hostname="host-a",
            failure_text="judgehost crashed\n\ntrace two",
            diagnostic_text="judgehost crashed\n\ntrace two",
            receipt_generation=receipt.claim_generation,
            diagnostic_limit_bytes=2048,
            updated_at="2026-08-11T00:00:03+00:00",
        )
        self.assertEqual(distinct.outcome, "late")
        diagnostics = scheduler.pending_case_diagnostics(int(case["id"]))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].text, "judgehost crashed\n\ntrace two")
        self.assertEqual(
            scheduler.fetch_batch(batch_id)["failure_runresult"],
            "internal-error",
        )

    def test_terminal_compile_success_only_fills_missing_runtime_evidence(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=150)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="late-compile-evidence",
            run_id="run-late-compile-evidence",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        self.assertEqual(
            _commit_leased_case_result(
                scheduler,
                case_id=int(case["id"]),
                hostname="host-a",
                result=_case_result("001.in"),
                updated_at="2026-08-11T00:00:01+00:00",
            ),
            "reported",
        )

        self.assertTrue(
            scheduler.record_compile_success(
                int(case["id"]),
                hostname="host-a",
                receipt_generation=_receipt_generation(
                    scheduler,
                    int(case["id"]),
                ),
                compile_output_b64="first-output",
                compile_metadata_b64="first-metadata",
                updated_at="2026-08-11T00:00:02+00:00",
            )
        )
        self.assertTrue(
            scheduler.record_compile_success(
                int(case["id"]),
                hostname="host-a",
                receipt_generation=_receipt_generation(
                    scheduler,
                    int(case["id"]),
                ),
                compile_output_b64="conflicting-output",
                compile_metadata_b64="conflicting-metadata",
                updated_at="2026-08-11T00:00:03+00:00",
            )
        )
        batch = scheduler.fetch_batch(batch_id)
        assert batch is not None
        self.assertEqual(batch["compile_output_b64"], "first-output")
        self.assertEqual(batch["compile_metadata_b64"], "first-metadata")

    def test_final_run_claim_routes_late_internal_error_to_diagnostic(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=148)
        case_row = _case_row(
            "final-before-internal-error",
            "run-final-before-internal-error",
            1,
            1,
        )
        case_row["verification_task_id"] = "vt-final-before-internal-error"
        batch_id = _create_ready_batch(
            scheduler,
            task_id="final-before-internal-error",
            run_id="run-final-before-internal-error",
            ordinals=[1],
            case_rows=[case_row],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-11T00:00:00+00:00",
        )[0]
        lease_generation = _receipt_generation(scheduler, int(case["id"]))
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=lease_generation,
            now_text="2026-08-11T00:00:01+00:00",
        )
        assert claim is not None

        internal_error = scheduler.claim_internal_error(
            claim.case_id,
            hostname="host-a",
            failure_text="late internal error",
            diagnostic_text="late internal error diagnostic",
            receipt_generation=lease_generation,
            diagnostic_limit_bytes=2048,
            updated_at="2026-08-11T00:00:02+00:00",
        )
        committed = scheduler.commit_case_result(
            claim.case_id,
            generation=claim.generation,
            result=_case_result("001.in"),
            updated_at="2026-08-11T00:00:03+00:00",
        )

        self.assertEqual(internal_error.outcome, "late")
        self.assertEqual(committed, "reported")
        persisted = scheduler.fetch_case(claim.case_id)
        assert persisted is not None
        self.assertEqual(str(persisted["runresult"]), "correct")
        batch = scheduler.fetch_batch(batch_id)
        assert batch is not None
        self.assertEqual(str(batch["failure_runresult"]), "")
        diagnostics = scheduler.pending_case_diagnostics(claim.case_id)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].kind, "internal-error")
        self.assertEqual(
            diagnostics[0].text,
            "late internal error diagnostic",
        )

    def test_host_release_does_not_reopen_sticky_compile_failure(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=149)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="released-compile-failure",
            run_id="run-released-compile-failure",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-05T00:00:00+00:00",
        )[0]
        compile_failure = scheduler.claim_compile_failure(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="compilation failed",
            compile_log="compilation failed",
            compile_diagnostics=(),
            updated_at="2026-08-05T00:00:01+00:00",
        )
        self.assertEqual(compile_failure.outcome, "claimed")
        self.assertEqual(scheduler.fetch_case(int(case["id"]))["status"], "reported")

        release = scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-05T00:00:02+00:00",
        )

        self.assertEqual(release.lease_count, 0)
        self.assertEqual(release.terminal_task_ids, ())
        self.assertEqual(release.workdirs, ())
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "succeeded")
        self.assertEqual(
            scheduler.fetch_batch(batch_id)["failure_runresult"],
            "internal-error",
        )
        self.assertEqual(scheduler.fetch_case(int(case["id"]))["status"], "reported")
        self.assertEqual(scheduler.batch_case_count(batch_id, status="pending"), 0)

    def test_undispatched_batch_precedes_dispatched_older_scope(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=140)
        older_id = _create_ready_batch(
            scheduler,
            task_id="older-claimed",
            run_id="run-older-claimed",
            ordinals=[1],
            scope=1,
            verification_id="ver-1",
        )
        newer_id = _create_ready_batch(
            scheduler,
            task_id="newer-unclaimed",
            run_id="run-newer-unclaimed",
            ordinals=[1],
            scope=2,
            verification_id="ver-2",
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], older_id)
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], newer_id)

    def test_direct_case_lease_does_not_create_host_affinity(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=155)
        first_id = _create_ready_batch(
            scheduler,
            task_id="direct-claimed",
            run_id="run-direct-claimed",
            ordinals=[1, 2],
        )
        _create_ready_batch(
            scheduler,
            task_id="direct-unclaimed",
            run_id="run-direct-unclaimed",
            ordinals=[3],
        )

        leased = scheduler.lease_cases(
            first_id,
            hostname="direct-host",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual([row["ordinal"] for row in leased], [1])
        self.assertEqual(scheduler.host_context_batches("direct-host"), [])
        self.assertEqual(scheduler.select_ready_batch("other-host")["batch_id"], first_id)

    def test_blocked_affinity_helps_same_verification_main_correct_first(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=165)
        solution_id = _create_ready_batch(
            scheduler,
            task_id="solution-a",
            run_id="run-solution-a",
            ordinals=[1],
            verification_id="ver-a",
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], solution_id)
        solution_case = scheduler.lease_cases(
            solution_id,
            hostname="host-a",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )[0]
        self.assertEqual(
            _commit_leased_case_result(
                scheduler,
                case_id=int(solution_case["id"]),
                hostname="host-a",
                result=_case_result("001.in"),
                updated_at=datetime.now(timezone.utc).isoformat(),
            ),
            "reported",
        )
        main_a = _create_ready_batch(
            scheduler,
            task_id="main-a",
            run_id="run-main-a",
            ordinals=[2],
            verification_id="ver-a",
            task_kind="main-correct",
        )
        generate_a = _create_ready_batch(
            scheduler,
            task_id="generate-a",
            run_id="run-generate-a",
            ordinals=[3],
            verification_id="ver-a",
            task_kind="generate-input",
        )
        main_b = _create_ready_batch(
            scheduler,
            task_id="main-b",
            run_id="run-main-b",
            ordinals=[1],
            verification_id="ver-b",
            task_kind="main-correct",
        )

        selected = scheduler.select_ready_batch("host-a")["batch_id"]
        self.assertEqual(selected, main_a)
        self.assertNotIn(selected, {generate_a, main_b})
        self.assertEqual(scheduler.batch_dispatch_count(main_a), 1)

    def test_full_affinity_queue_does_not_create_stolen_state(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=185)
        affinity_ids: list[int] = []
        for index in range(4):
            batch_id = _create_ready_batch(
                scheduler,
                task_id=f"affinity-{index}",
                run_id=f"run-affinity-{index}",
                ordinals=[index + 1],
            )
            affinity_ids.append(batch_id)
            self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
            case = scheduler.lease_cases(
                batch_id,
                hostname="host-a",
                limit=1,
                now_text=datetime.now(timezone.utc).isoformat(),
            )[0]
            self.assertEqual(
                _commit_leased_case_result(
                    scheduler,
                    case_id=int(case["id"]),
                    hostname="host-a",
                    result=_case_result(str(case["test_name"])),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ),
                "reported",
            )

        stolen_id = _create_ready_batch(
            scheduler,
            task_id="stolen",
            run_id="run-stolen",
            ordinals=[10],
        )
        waiting_id = _create_ready_batch(
            scheduler,
            task_id="waiting",
            run_id="run-waiting",
            ordinals=[11],
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], stolen_id)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], waiting_id)
        self.assertEqual(
            [row["batch_id"] for row in scheduler.host_context_batches("host-a")],
            affinity_ids,
        )

    def test_batch_and_case_ids_share_one_collision_free_namespace(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=150)
        batch_id, _now_text = _create_staged_batch(
            scheduler,
            task_id="identity",
            run_id="run-identity",
            ordinals=[1],
        )
        case_id = int(scheduler.cases_for_batch(batch_id)[0]["id"])

        self.assertEqual(batch_id, 151)
        self.assertEqual(case_id, 152)

    def test_protocol_ids_follow_verification_and_compile_identity(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=175)
        first_key = "1" * 64
        second_key = "2" * 64
        first_id, _ = _create_staged_batch(
            scheduler,
            task_id="identity-a",
            run_id="run-identity-a",
            ordinals=[1],
            verification_id="ver-123",
            compile_key=first_key,
        )
        second_id, _ = _create_staged_batch(
            scheduler,
            task_id="identity-b",
            run_id="run-identity-b",
            ordinals=[2],
            verification_id="ver-123",
            compile_key=second_key,
        )
        third_id, _ = _create_staged_batch(
            scheduler,
            task_id="identity-c",
            run_id="run-identity-c",
            ordinals=[3],
            verification_id="ver-124",
            compile_key=first_key,
        )

        first = scheduler.fetch_batch(first_id)
        second = scheduler.fetch_batch(second_id)
        third = scheduler.fetch_batch(third_id)
        assert first is not None and second is not None and third is not None
        self.assertEqual(first["job_id"], 0x123)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertNotEqual(third["job_id"], first["job_id"])
        self.assertEqual(first["compile_key"], third["compile_key"])
        self.assertNotEqual(first["compile_key"], second["compile_key"])
        first_submission = scheduler.compile_submission_for_batch(first_id)
        third_submission = scheduler.compile_submission_for_batch(third_id)
        assert first_submission is not None and third_submission is not None
        self.assertEqual(first_submission.submit_id, third_submission.submit_id)

    def test_protocol_id_collisions_fail_fast(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=190)
        low_key = f"{1:064x}"
        colliding_key = f"{(1 << 63) + 1:064x}"
        _create_staged_batch(
            scheduler,
            task_id="collision-a",
            run_id="run-collision-a",
            ordinals=[1],
            verification_id="ver-1",
            compile_key=low_key,
        )
        with self.assertRaisesRegex(RuntimeError, "submit id collision"):
            _create_staged_batch(
                scheduler,
                task_id="collision-b",
                run_id="run-collision-b",
                ordinals=[2],
                verification_id="ver-2",
                compile_key=colliding_key,
            )

    def test_ready_batch_claim_does_not_consume_untransitioned_work(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=200)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="retryable",
            run_id="run-retryable",
            ordinals=[1],
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], batch_id)

    def test_ready_wait_uses_generation_without_lost_wakeup(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=260)
        _batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="wait-ready",
            run_id="run-wait-ready",
            ordinals=[1],
        )
        entered = threading.Event()
        outcomes: list[bool] = []

        def _wait() -> None:
            entered.set()
            outcomes.append(scheduler.wait_for_ready_batch(1.0))

        thread = threading.Thread(target=_wait)
        thread.start()
        self.assertTrue(entered.wait(timeout=1.0))
        scheduler.activate_task_cases("wait-ready", now_text=now_text)
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [True])
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], _batch_id)

    def test_reporting_claim_serializes_duplicate_and_defers_cancel(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=250)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="reporting",
            run_id="run-reporting",
            ordinals=[1],
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text="2026-08-03T00:00:00+00:00",
        )[0]
        case_id = int(case["id"])
        claim = scheduler.claim_case_reporting(
            case_id,
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, case_id),
            now_text="2026-08-03T00:00:01+00:00",
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(CaseClaimBusy):
            scheduler.claim_case_reporting(
                case_id,
                hostname="host-a",
                receipt_generation=_receipt_generation(scheduler, case_id),
                now_text="2026-08-03T00:00:02+00:00",
            )
        cancellation = scheduler.request_verification_cancel(
            "ver-1",
            now_text="2026-08-03T00:00:03+00:00",
        )
        self.assertEqual(list(cancellation.batch_ids), [batch_id])
        self.assertFalse(scheduler.task_cases_terminal("reporting"))
        release = scheduler.release_host_leases(
            "host-a",
            now_text="2026-08-03T00:00:03+00:00",
        )
        self.assertEqual(release.lease_count, 1)
        self.assertEqual(scheduler.fetch_case(case_id)["status"], "reporting")
        internal_error = scheduler.claim_internal_error(
            case_id,
            hostname="host-a",
            failure_text="late internal error",
            diagnostic_text="late internal error",
            receipt_generation=claim.generation,
            diagnostic_limit_bytes=2048,
            updated_at="2026-08-03T00:00:03+00:00",
        )
        self.assertEqual(internal_error.outcome, "late")
        self.assertEqual(
            scheduler.commit_case_result(
                case_id,
                generation=claim.generation,
                result=_case_result("001.in"),
                updated_at="2026-08-03T00:00:04+00:00",
            ),
            "cancelled",
        )
        self.assertTrue(scheduler.task_cases_terminal("reporting"))
        scheduler.finish_verification_execution("ver-1", now_text="2026-08-03T00:00:04+00:00")
        self.assertEqual(scheduler.fetch_batch(batch_id)["status"], "finalize-pending")

    def test_unknown_compile_batch_can_lease_to_multiple_hosts(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=275)
        batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="warm-compile",
            run_id="run-warm-compile",
            ordinals=[1, 2, 3],
        )
        scheduler.activate_task_cases("warm-compile", now_text=now_text)
        for cache_claim, _row in scheduler.claim_cache_cases(
            batch_id,
            hostname="cache",
            limit=3,
            now_text=now_text,
        ):
            scheduler.finish_cache_miss(
                cache_claim.case_id,
                generation=cache_claim.generation,
                updated_at=now_text,
            )
        scheduler.claim_materialization(batch_id, now_text=now_text)
        scheduler.finish_materialization(
            batch_id,
            success=True,
            error_text="",
            now_text=now_text,
        )

        first = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=now_text,
        )
        second = scheduler.lease_cases(
            batch_id,
            hostname="host-b",
            limit=1,
            now_text=now_text,
        )
        self.assertEqual([int(row["ordinal"]) for row in first], [1])
        self.assertEqual([int(row["ordinal"]) for row in second], [2])
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "unknown")

        case_id = int(first[0]["id"])
        claim = scheduler.claim_case_reporting(
            case_id,
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, case_id),
            now_text=now_text,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertTrue(
            scheduler.observe_compile_success_from_case_claim(
                case_id,
                generation=claim.generation,
                lease_owner="host-a",
                updated_at=now_text,
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "succeeded")
        followers = scheduler.lease_cases(
            batch_id,
            hostname="host-c",
            limit=8,
            now_text=now_text,
        )
        self.assertEqual([int(row["ordinal"]) for row in followers], [3])

    def test_invalid_run_result_claim_cannot_change_compile_state(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=290)
        batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="stale-compile",
            run_id="run-stale-compile",
            ordinals=[1],
        )
        scheduler.activate_task_cases("stale-compile", now_text=now_text)
        cache_claim, _row = scheduler.claim_cache_cases(
            batch_id,
            hostname="cache",
            limit=1,
            now_text=now_text,
        )[0]
        scheduler.finish_cache_miss(
            cache_claim.case_id,
            generation=cache_claim.generation,
            updated_at=now_text,
        )
        scheduler.claim_materialization(batch_id, now_text=now_text)
        scheduler.finish_materialization(
            batch_id,
            success=True,
            error_text="",
            now_text=now_text,
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=1,
            now_text=now_text,
        )[0]
        claim = scheduler.claim_case_reporting(
            int(case["id"]),
            hostname="host-a",
            receipt_generation=_receipt_generation(scheduler, int(case["id"])),
            now_text=now_text,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertFalse(
            scheduler.observe_compile_success_from_case_claim(
                claim.case_id,
                generation=claim.generation + 1,
                lease_owner="host-a",
                updated_at=now_text,
            )
        )
        self.assertFalse(
            scheduler.observe_compile_success_from_case_claim(
                claim.case_id,
                generation=claim.generation,
                lease_owner="host-b",
                updated_at=now_text,
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "unknown")
        compile_failure = scheduler.claim_compile_failure(
            claim.case_id,
            hostname="host-a",
            receipt_generation=claim.generation,
            compile_output_b64="",
            compile_metadata_b64="",
            failure_text="late compilation failure",
            compile_log="late compilation failure",
            compile_diagnostics=(),
            updated_at=now_text,
        )
        self.assertEqual(compile_failure.outcome, "late")
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "unknown")
        self.assertTrue(
            scheduler.record_compile_success(
                claim.case_id,
                hostname="host-a",
                receipt_generation=claim.generation,
                compile_output_b64="",
                compile_metadata_b64="",
                updated_at=now_text,
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "succeeded")

    def test_cache_case_claims_are_exclusive_and_never_become_host_leases(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=300)
        batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="bulk",
            run_id="run-bulk",
            ordinals=list(range(1, 257)),
        )

        self.assertTrue(scheduler.activate_task_cases("bulk", now_text=now_text))
        claims = scheduler.claim_cache_cases(
            batch_id,
            hostname="cache-a",
            limit=256,
            now_text=now_text,
        )
        self.assertEqual(len(claims), 256)
        self.assertEqual(
            scheduler.claim_cache_cases(
                batch_id,
                hostname="cache-b",
                limit=256,
                now_text=now_text,
            ),
            [],
        )
        first_claim, _first_row = claims[0]
        self.assertEqual(
            scheduler.commit_case_result(
                first_claim.case_id,
                generation=first_claim.generation,
                result=_case_result("001.in"),
                updated_at=now_text,
            ),
            "reported",
        )
        reported = scheduler.fetch_case(first_claim.case_id)
        self.assertEqual(reported["status"], "reported")
        self.assertIsNone(reported["lease_owner"])
        outcomes = scheduler.finish_cache_claims(
            [(claim, None) for claim, _row in claims[1:]],
            updated_at=now_text,
        )
        self.assertEqual(set(outcomes.values()), {"pending"})
        self.assertEqual(len(scheduler.cases_for_batch(batch_id, status="pending")), 255)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)

    def test_bulk_cache_abort_refreshes_each_batch_once(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=315)
        batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="bulk-abort",
            run_id="run-bulk-abort",
            ordinals=list(range(1, 258)),
        )
        self.assertTrue(scheduler.activate_task_cases("bulk-abort", now_text=now_text))
        claims = scheduler.claim_cache_cases(
            batch_id,
            hostname="cache",
            limit=256,
            now_text=now_text,
        )
        self.assertEqual(len(claims), 256)
        first_claim, _row = claims[0]
        self.assertTrue(
            scheduler.finish_cache_miss(
                first_claim.case_id,
                generation=first_claim.generation,
                updated_at=now_text,
            )
        )
        self.assertEqual(
            scheduler.abort_cache_claims(
                [claim for claim, _row in claims[1:]],
                updated_at=now_text,
            ),
            255,
        )
        self.assertEqual(scheduler.batch_case_count(batch_id, status="pending"), 1)
        self.assertEqual(
            scheduler.batch_case_count(batch_id, status="cache-pending"),
            256,
        )

    def test_script_hash_index_tracks_open_job_references(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=400)
        first_batch, now_text = _create_staged_batch(
            scheduler,
            task_id="first-script-user",
            run_id="run-first-script-user",
            ordinals=[1],
        )
        second_batch, _ = _create_staged_batch(
            scheduler,
            task_id="second-script-user",
            run_id="run-second-script-user",
            ordinals=[1],
        )
        self.assertTrue(scheduler.activate_task_cases("first-script-user", now_text=now_text))
        self.assertTrue(scheduler.activate_task_cases("second-script-user", now_text=now_text))
        compile_hash = "1" * 32
        compile_id = int(script_id(compile_hash))

        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), {compile_hash})
        for batch_id in (first_batch, second_batch):
            self.assertTrue(
                scheduler.record_batch_failure(
                    batch_id,
                    runresult="internal-error",
                    error_text="script resolution failed",
                    updated_at=now_text,
                )
            )
            self.assertEqual(scheduler.active_script_hashes("compile", compile_id), {compile_hash})
        self.assertCountEqual(
            scheduler.finish_verification_execution("ver-1", now_text=now_text),
            [first_batch, second_batch],
        )
        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), set())
        self.assertIsNotNone(
            scheduler.claim_batch_finalization(
                first_batch,
                now_text=now_text,
            )
        )
        self.assertIsNotNone(
            scheduler.claim_batch_finalization(
                second_batch,
                now_text=now_text,
            )
        )
        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), set())

    def test_sixteen_hosts_never_lease_a_case_twice(self) -> None:
        scheduler = JudgehostBatchRuntime(id_base=1000)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="shared",
            run_id="run-shared",
            ordinals=list(range(1, 257)),
        )
        leased_ids: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def _worker(worker: int) -> None:
            hostname = f"host-{worker}"
            barrier.wait(timeout=5)
            batch = scheduler.select_ready_batch(hostname)
            if batch is None:
                return
            rows = scheduler.lease_cases(
                int(batch["batch_id"]),
                hostname=hostname,
                limit=16,
                now_text=datetime.now(timezone.utc).isoformat(),
            )
            with lock:
                leased_ids.extend(int(row["id"]) for row in rows)

        threads = [threading.Thread(target=_worker, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(leased_ids), 256)
        self.assertEqual(len(set(leased_ids)), 256)
        self.assertEqual(scheduler.host_leased_case_count("host-0") >= 0, True)
        self.assertEqual(len(scheduler.cases_for_batch(batch_id, status="leased")), 256)

    def test_judgehost_runtime_defaults_and_overrides(self) -> None:
        defaults = build_config_values()
        self.assertEqual(defaults.JUDGEHOST_FETCH_BATCH_SIZE, 2)
        self.assertEqual(defaults.RUN_EXEC_PROCESS_LIMIT, 1024)
        overridden = build_config_values(
            {"JUDGEHOST_FETCH_BATCH_SIZE": 8, "RUN_EXEC_PROCESS_LIMIT": 256}
        )
        self.assertEqual(overridden.JUDGEHOST_FETCH_BATCH_SIZE, 8)
        self.assertEqual(overridden.RUN_EXEC_PROCESS_LIMIT, 256)

    def test_runtime_cache_hit_does_not_open_payload_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-cache-") as temp_dir:
            _toolkit, _blobs, cache = self._runtime_toolkit(Path(temp_dir))
            cache.put(
                namespace=RuntimeCacheIndex.RESULT,
                key_hash="1" * 64,
                signature="2" * 64,
                value={"verdict": "OK"},
                files={"program.out": b"large output"},
            )
            with patch.object(Path, "open", side_effect=AssertionError("payload was opened")):
                entry = cache.get(
                    namespace=RuntimeCacheIndex.RESULT,
                    key_hash="1" * 64,
                    signature="2" * 64,
                )
            self.assertIsNotNone(entry)

    def test_cache_deletion_keeps_referenced_blob(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-cache-delete-") as temp_dir:
            _toolkit, blobs, cache = self._runtime_toolkit(Path(temp_dir))
            entry = cache.put(
                namespace=RuntimeCacheIndex.RESULT,
                key_hash="3" * 64,
                signature="4" * 64,
                value={},
                files={"program.out": b"answer\n"},
            )
            output = entry.files["program.out"]
            cache.delete(
                namespace=RuntimeCacheIndex.RESULT,
                key_hash="3" * 64,
                signature="4" * 64,
            )
            self.assertIsNone(
                cache.get(
                    namespace=RuntimeCacheIndex.RESULT,
                    key_hash="3" * 64,
                    signature="4" * 64,
                )
            )
            self.assertEqual(blobs.read(output), b"answer\n")

    def test_executable_cache_reuses_valid_entry_and_repairs_damage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-executable-cache-") as temp_dir:
            toolkit, _blobs, cache = self._runtime_toolkit(Path(temp_dir))
            executable_hash = "a" * 32
            files = [("run", b"#!/bin/sh\nexit 0\n", True)]
            with patch.object(cache, "put", wraps=cache.put) as publish_entry:
                toolkit.store_executable_cache(
                    kind="run",
                    executable_hash=executable_hash,
                    files=files,
                )
                toolkit.store_executable_cache(
                    kind="run",
                    executable_hash=executable_hash,
                    files=files,
                )
            self.assertEqual(publish_entry.call_count, 2)
            rows = toolkit.read_executable_cache(kind="run", executable_hash=executable_hash)
            self.assertIsNotNone(rows)
            assert rows is not None
            self.assertEqual(rows[0]["payload"].path.read_bytes(), files[0][1])

            key_hash = toolkit._executable_cache_key_hash("run", executable_hash)
            entry = cache.get(
                namespace=RuntimeCacheIndex.EXECUTABLE,
                key_hash=key_hash,
                signature=toolkit._EXECUTABLE_CACHE_SIGNATURE,
            )
            assert entry is not None
            executable_path = entry.files["run"].path
            executable_path.unlink()
            self.assertIsNone(
                toolkit.read_executable_cache(kind="run", executable_hash=executable_hash)
            )
            toolkit.store_executable_cache(
                kind="run",
                executable_hash=executable_hash,
                files=files,
            )
            self.assertEqual(executable_path.read_bytes(), files[0][1])

    def test_different_cache_keys_publish_concurrently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-cache-parallel-") as temp_dir:
            _toolkit, blobs, cache = self._runtime_toolkit(Path(temp_dir))
            barrier = threading.Barrier(2)
            errors: list[Exception] = []
            original = blobs.put_bytes

            def _publish(payload: bytes):
                barrier.wait(timeout=5)
                return original(payload)

            def _put(token: str) -> None:
                try:
                    cache.put(
                        namespace=RuntimeCacheIndex.RESULT,
                        key_hash=token * 64,
                        signature=("f" if token == "1" else "e") * 64,
                        value={"token": token},
                        files={"program.out": token.encode("ascii")},
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with patch.object(blobs, "put_bytes", side_effect=_publish):
                threads = [threading.Thread(target=_put, args=(token,)) for token in ("1", "2")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(cache.count_entries(namespace=RuntimeCacheIndex.RESULT), 2)

    def test_terminal_cleanup_removes_runtime_identity_but_not_cache(self) -> None:
        registry = JudgehostTaskRegistry()
        row = _task_row(1, verification_id="ver-c1ea4")
        row["status"] = "completed"
        row["verification_task_id"] = "verification-task-1"
        registry.insert(row)

        class _CaseStore:
            def __init__(self) -> None:
                self.forgotten_runs: list[str] = []
                self.forgotten_scopes: list[str] = []

            def forget_runs_if_quiet(self, run_ids: list[str]) -> int | None:
                self.forgotten_runs.extend(run_ids)
                return len(run_ids)

            def forget_scope(self, verification_id: str) -> None:
                self.forgotten_scopes.append(verification_id)

        cases = _CaseStore()

        class _VerificationRuntimeStore:
            def __init__(self) -> None:
                self.unbound: list[tuple[str, str]] = []

            def unbind(
                self,
                verification_task_id: str,
                *,
                judgehost_task_id: str,
            ) -> bool:
                self.unbound.append((verification_task_id, judgehost_task_id))
                return True

        runtimes = _VerificationRuntimeStore()
        cleanup = JudgehostTerminalCleanup(registry, cases, runtimes)
        cleanup._generation_by_verification["ver-c1ea4"] = 2
        self.assertTrue(cleanup._cleanup("ver-c1ea4", expected_generation=2))
        self.assertEqual(cases.forgotten_runs, ["run-1"])
        self.assertEqual(cases.forgotten_scopes, ["ver-c1ea4"])
        self.assertEqual(
            runtimes.unbound,
            [("verification-task-1", "task-1")],
        )
        self.assertIsNone(registry.get("task-1"))


if __name__ == "__main__":
    unittest.main()
