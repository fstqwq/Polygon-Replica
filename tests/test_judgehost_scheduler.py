from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.runtime_value import build_runtime_values
from app.service.judgehost.cleanup import JudgehostTerminalCleanup
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.domjudge.client import domjudge_script_id
from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import (
    CaseClaimBusy,
    CompileSubmission,
    ExecutionBatchSpec,
)
from app.service.judgehost.identity import domjudge_submit_id
from app.service.judgehost.task_registry import JudgehostTaskRegistry
from app.service.judgehost.toolkit import DomjudgeToolkit
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


def _case_result(test_name: str, *, runresult: str = "correct", verdict: str = "OK"):
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


_COMPILE_KEY = "5" * 64


def _submission_for_key(compile_key: str) -> CompileSubmission:
    return CompileSubmission(
        compile_key=compile_key,
        submit_id=domjudge_submit_id(compile_key),
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
    scheduler: BatchScheduler,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
    case_rows: list[dict[str, object]] | None = None,
    verification_id: str = "ver-1",
    compile_key: str = _COMPILE_KEY,
    execution_signature: str | None = None,
    logical_run_id: str | None = None,
    task_kind: str = "solution-run",
) -> tuple[int, str]:
    now_text = datetime.now(timezone.utc).isoformat()
    signature = hashlib.sha256(
        (execution_signature or f"signature-{task_id}").encode("utf-8")
    ).hexdigest()
    batch_id = scheduler.create_batch_with_cases(
        task_id=task_id,
        run_id=run_id,
        logical_run_id=logical_run_id or run_id,
        execution_signature=signature,
        task_kind=task_kind,
        verification_id=verification_id,
        compile_key=compile_key,
        compile_submission=_submission_for_key(compile_key),
        contest_id="local",
        mode="pass-fail",
        source_name="main.cpp",
        compile_hash="1" * 32,
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
    scheduler: BatchScheduler,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
    verification_id: str = "ver-1",
    task_kind: str = "solution-run",
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
    scheduler.record_compile_result(
        batch_id,
        compile_success=1,
        compile_output_b64="",
        compile_metadata_b64="",
        lease_owner="host-setup",
        updated_at=now_text,
    )
    return batch_id


class TestJudgehostScheduler(unittest.TestCase):
    @staticmethod
    def _runtime_toolkit(
        temp_root: Path,
    ) -> tuple[DomjudgeToolkit, RuntimeBlobStore, RuntimeCacheIndex]:
        blob_store = RuntimeBlobStore(temp_root)
        cache_index = RuntimeCacheIndex(blob_store)
        state = SimpleNamespace(
            runtime_blob_store=blob_store,
            runtime_cache_index=cache_index,
        )
        return (DomjudgeToolkit(state), blob_store, cache_index)

    def test_materialized_compile_source_outlives_snapshot_descriptor(self) -> None:
        scheduler = BatchScheduler(id_base=100)
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
            blob_store = RuntimeBlobStore(root / "runtime")
            stored_source = blob_store.put_file(
                RuntimeBlobStore.describe_file(snapshot_source)
            )
            materialized = CompileSubmission(
                compile_key=_COMPILE_KEY,
                submit_id=domjudge_submit_id(_COMPILE_KEY),
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

    def test_task_registry_has_identity_but_no_scheduler(self) -> None:
        registry = JudgehostTaskRegistry()
        registry.insert(_task_row(1))
        self.assertEqual(registry.get_for_run("run-1")["id"], "task-1")
        self.assertFalse(hasattr(registry, "claim_ready"))
        self.assertFalse(hasattr(registry, "renew"))

    def test_invalid_case_spec_does_not_partially_create_batch(self) -> None:
        scheduler = BatchScheduler(id_base=50)
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

        with patch("app.service.judgehost.batch_scheduler.domjudge_script_id", side_effect=RuntimeError("invalid script hash")):
            with self.assertRaisesRegex(RuntimeError, "invalid script hash"):
                _create_staged_batch(
                    scheduler,
                    task_id="invalid-script",
                    run_id="run-invalid-script",
                    ordinals=[1],
                )
        self.assertIsNone(scheduler.fetch_batch(51))

    def test_ready_batch_prefers_foreground_then_numeric_test_order(self) -> None:
        scheduler = BatchScheduler(id_base=100)
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
        scheduler = BatchScheduler(id_base=125)
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

    def test_host_queue_keeps_temporarily_blocked_batch(self) -> None:
        scheduler = BatchScheduler(id_base=132)
        first_id = _create_ready_batch(
            scheduler,
            task_id="affinity-first",
            run_id="run-affinity-first",
            ordinals=[1],
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
        scheduler.request_batch_case_results(
            first_id,
            results={int(first_case["id"]): _case_result("001.in")},
            updated_at=datetime.now(timezone.utc).isoformat(),
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
            logical_run_id="run-affinity-first",
        )
        self.assertEqual(first_again, first_id)
        scheduler.activate_task_cases("affinity-first-later", now_text=now_text)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)

    def test_host_release_clears_local_queue_without_resetting_dispatch_order(self) -> None:
        scheduler = BatchScheduler(id_base=138)
        first_id = _create_ready_batch(
            scheduler,
            task_id="released-affinity",
            run_id="run-released-affinity",
            ordinals=[1],
        )
        other_id = _create_ready_batch(
            scheduler,
            task_id="other-affinity",
            run_id="run-other-affinity",
            ordinals=[2],
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], first_id)
        scheduler.lease_cases(
            first_id,
            hostname="host-a",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )

        release = scheduler.release_host_leases(
            "host-a",
            now_text=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual((release.affinity_count, release.lease_count), (1, 1))
        self.assertEqual(scheduler.host_context_batches("host-a"), [])
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], other_id)
        self.assertEqual(scheduler.select_ready_batch("host-c")["batch_id"], first_id)

    def test_compile_failure_stays_on_unique_execution_batch(self) -> None:
        scheduler = BatchScheduler(id_base=144)
        batch_id, now_text = _create_staged_batch(
            scheduler,
            task_id="failed-first",
            run_id="run-failed-first",
            ordinals=[1],
            execution_signature="sticky-failure",
        )
        scheduler.record_compile_result(
            batch_id,
            compile_success=0,
            compile_output_b64="",
            compile_metadata_b64="",
            lease_owner="host-a",
            updated_at=now_text,
        )

        same_batch_id, _ = _create_staged_batch(
            scheduler,
            task_id="failed-later",
            run_id="run-failed-later",
            ordinals=[2],
            execution_signature="sticky-failure",
            logical_run_id="run-failed-first",
        )

        self.assertEqual(same_batch_id, batch_id)
        self.assertEqual(scheduler.fetch_batch(batch_id)["failure_runresult"], "compiler-error")
        self.assertEqual(len(scheduler.cases_for_batch(batch_id)), 2)
        self.assertFalse(
            scheduler.record_compile_result(
                batch_id,
                compile_success=1,
                compile_output_b64="",
                compile_metadata_b64="",
                lease_owner="host-a",
                updated_at=now_text,
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "failed")

    def test_undispatched_batch_precedes_dispatched_older_scope(self) -> None:
        scheduler = BatchScheduler(id_base=140)
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
        scheduler = BatchScheduler(id_base=155)
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
        scheduler = BatchScheduler(id_base=165)
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
        scheduler.request_batch_case_results(
            solution_id,
            results={int(solution_case["id"]): _case_result("001.in")},
            updated_at=datetime.now(timezone.utc).isoformat(),
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

    def test_full_affinity_queue_keeps_one_stolen_batch_until_it_blocks(self) -> None:
        scheduler = BatchScheduler(id_base=185)
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
            scheduler.request_batch_case_results(
                batch_id,
                results={int(case["id"]): _case_result(str(case["test_name"]))},
                updated_at=datetime.now(timezone.utc).isoformat(),
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
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], stolen_id)
        self.assertEqual(len(scheduler.host_context_batches("host-a")), 5)

        stolen_case = scheduler.lease_cases(
            stolen_id,
            hostname="host-a",
            limit=1,
            now_text=datetime.now(timezone.utc).isoformat(),
        )[0]
        scheduler.request_batch_case_results(
            stolen_id,
            results={int(stolen_case["id"]): _case_result("010.in")},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], waiting_id)
        self.assertEqual(
            [row["batch_id"] for row in scheduler.host_context_batches("host-a")[:4]],
            affinity_ids,
        )

    def test_batch_and_case_ids_share_one_collision_free_namespace(self) -> None:
        scheduler = BatchScheduler(id_base=150)
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
        scheduler = BatchScheduler(id_base=175)
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
        self.assertEqual(first["domjudge_job_id"], 0x123)
        self.assertEqual(second["domjudge_job_id"], first["domjudge_job_id"])
        self.assertNotEqual(third["domjudge_job_id"], first["domjudge_job_id"])
        self.assertEqual(first["compile_key"], third["compile_key"])
        self.assertNotEqual(first["compile_key"], second["compile_key"])
        first_submission = scheduler.compile_submission_for_batch(first_id)
        third_submission = scheduler.compile_submission_for_batch(third_id)
        assert first_submission is not None and third_submission is not None
        self.assertEqual(first_submission.submit_id, third_submission.submit_id)

    def test_protocol_id_collisions_fail_fast(self) -> None:
        scheduler = BatchScheduler(id_base=190)
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
        with self.assertRaisesRegex(RuntimeError, "batch id collision"):
            _create_staged_batch(
                scheduler,
                task_id="collision-c",
                run_id="run-collision-c",
                ordinals=[3],
                verification_id="ver-8000000000000001",
                compile_key=low_key,
            )

    def test_ready_batch_claim_does_not_consume_untransitioned_work(self) -> None:
        scheduler = BatchScheduler(id_base=200)
        batch_id = _create_ready_batch(
            scheduler,
            task_id="retryable",
            run_id="run-retryable",
            ordinals=[1],
        )

        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)
        self.assertEqual(scheduler.select_ready_batch("host-b")["batch_id"], batch_id)

    def test_reporting_claim_serializes_duplicate_and_defers_cancel(self) -> None:
        scheduler = BatchScheduler(id_base=250)
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
            now_text="2026-08-03T00:00:01+00:00",
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        with self.assertRaises(CaseClaimBusy):
            scheduler.claim_case_reporting(
                case_id,
                hostname="host-a",
                now_text="2026-08-03T00:00:02+00:00",
            )
        cancellation = scheduler.request_verification_cancel(
            "ver-1",
            now_text="2026-08-03T00:00:03+00:00",
        )
        self.assertEqual(list(cancellation.batch_ids), [batch_id])
        self.assertFalse(scheduler.task_cases_terminal("reporting"))
        scheduler.request_batch_case_results(
            batch_id,
            results={
                case_id: _case_result(
                    "001.in",
                    runresult="internal-error",
                    verdict="FL",
                )
            },
            updated_at="2026-08-03T00:00:03+00:00",
        )
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

    def test_run_result_claim_opens_warm_compile_gate_without_compile_callback(self) -> None:
        scheduler = BatchScheduler(id_base=275)
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

        leader = scheduler.lease_cases(
            batch_id,
            hostname="host-a",
            limit=8,
            now_text=now_text,
        )
        self.assertEqual(len(leader), 1)
        case_id = int(leader[0]["id"])
        claim = scheduler.claim_case_reporting(
            case_id,
            hostname="host-a",
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
            hostname="host-b",
            limit=8,
            now_text=now_text,
        )
        self.assertEqual([int(row["ordinal"]) for row in followers], [2, 3])

    def test_invalid_run_result_claim_cannot_change_compile_state(self) -> None:
        scheduler = BatchScheduler(id_base=290)
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
        self.assertTrue(
            scheduler.record_compile_result(
                batch_id,
                compile_success=0,
                compile_output_b64="",
                compile_metadata_b64="",
                lease_owner="host-a",
                updated_at=now_text,
            )
        )
        self.assertFalse(
            scheduler.observe_compile_success_from_case_claim(
                claim.case_id,
                generation=claim.generation,
                lease_owner="host-a",
                updated_at=now_text,
            )
        )
        self.assertEqual(scheduler.fetch_batch(batch_id)["compile_state"], "failed")

    def test_cache_case_claims_are_exclusive_and_never_become_host_leases(self) -> None:
        scheduler = BatchScheduler(id_base=300)
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
        for claim, _row in claims[1:]:
            self.assertTrue(
                scheduler.finish_cache_miss(
                    claim.case_id,
                    generation=claim.generation,
                    updated_at=now_text,
                )
            )
        self.assertEqual(len(scheduler.cases_for_batch(batch_id, status="pending")), 255)
        self.assertEqual(scheduler.select_ready_batch("host-a")["batch_id"], batch_id)

    def test_script_hash_index_tracks_open_job_references(self) -> None:
        scheduler = BatchScheduler(id_base=400)
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
        compile_hash = "1" * 32
        compile_id = int(domjudge_script_id(compile_hash))

        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), {compile_hash})
        for batch_id in (first_batch, second_batch):
            case = scheduler.cases_for_batch(batch_id)[0]
            scheduler.request_batch_case_results(
                batch_id,
                results={
                    int(case["id"]): _case_result(
                        str(case["test_name"]),
                        runresult="internal-error",
                        verdict="FL",
                    )
                },
                updated_at=now_text,
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
        scheduler = BatchScheduler(id_base=1000)
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
        defaults = build_runtime_values()
        self.assertEqual(defaults.JUDGEHOST_FETCH_BATCH_SIZE, 2)
        self.assertEqual(defaults.RUN_EXEC_PROCESS_LIMIT, 1024)
        overridden = build_runtime_values(
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
            self.assertIsNone(cache.get(
                namespace=RuntimeCacheIndex.RESULT,
                key_hash="3" * 64,
                signature="4" * 64,
            ))
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
        registry.insert(row)

        class _CaseStore:
            def __init__(self) -> None:
                self.forgotten_runs: list[str] = []
                self.forgotten_scopes: list[str] = []

            def forget_runs(self, run_ids: list[str]) -> int:
                self.forgotten_runs.extend(run_ids)
                return len(run_ids)

            def forget_scope(self, verification_id: str) -> None:
                self.forgotten_scopes.append(verification_id)

        cases = _CaseStore()
        cleanup = JudgehostTerminalCleanup(registry, cases)
        cleanup._generation_by_verification["ver-c1ea4"] = 2
        self.assertTrue(cleanup._cleanup("ver-c1ea4", expected_generation=2))
        self.assertEqual(cases.forgotten_runs, ["run-1"])
        self.assertEqual(cases.forgotten_scopes, ["ver-c1ea4"])
        self.assertIsNone(registry.get("task-1"))


if __name__ == "__main__":
    unittest.main()
