from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.runtime_value import build_runtime_values
from app.service.judgehost.cleanup import JudgehostTerminalCleanup
from app.service.judgehost.domjudge.cache import domjudge_hash_of_hashes
from app.service.judgehost.domjudge.client import domjudge_script_id
from app.service.judgehost.job_scheduler import JobScheduler
from app.service.judgehost.job_scheduler_models import JobSpec
from app.service.judgehost.task_registry import JudgehostTaskRegistry
from app.service.judgehost.toolkit import DomjudgeToolkit
from app.service.platform.hashing import sha256_hex_bytes
from app.service.platform.judge_fs_index import JudgeFsIndexService


def _task_row(index: int, *, verification_id: str = "verification") -> dict[str, object]:
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
        "input_ref": f"cache://input/{ordinal}",
        "answer_ref": f"cache://answer/{ordinal}",
        "status": "staged",
    }


def _create_staged_job(
    scheduler: JobScheduler,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
    case_rows: list[dict[str, object]] | None = None,
) -> tuple[int, str]:
    now_text = datetime.now(timezone.utc).isoformat()
    job_id = scheduler.create_job_with_cases(
        task_id=task_id,
        run_id=run_id,
        group_key=f"group-{task_id}",
        contest_id="local",
        mode="pass-fail",
        source_name="main.cpp",
        source_path="",
        work_root="",
        compile_hash="1" * 32,
        run_hash="2" * 32,
        compare_hash="3" * 32,
        source_hash="4" * 64,
        compile_config_json="{}",
        run_config_json="{}",
        compare_config_json="{}",
        expected_behavior="accepted",
        verification_source="solution-run",
        force_recompile=0,
        service_class=service_class,
        job_spec=JobSpec(),
        created_at=now_text,
        case_rows=(
            [_case_row(task_id, run_id, ordinal, scope) for ordinal in ordinals]
            if case_rows is None
            else case_rows
        ),
    )
    return job_id, now_text


def _create_ready_job(
    scheduler: JobScheduler,
    *,
    task_id: str,
    run_id: str,
    ordinals: list[int],
    service_class: str = "background",
    scope: int = 1,
) -> int:
    job_id, now_text = _create_staged_job(
        scheduler,
        task_id=task_id,
        run_id=run_id,
        ordinals=ordinals,
        service_class=service_class,
        scope=scope,
    )
    scheduler.activate_task_cases(task_id, now_text=now_text)
    cache_rows = scheduler.cache_pending_cases(job_id)
    scheduler.mark_cache_misses([int(row["id"]) for row in cache_rows], now_text=now_text)
    scheduler.claim_materialization(job_id, now_text=now_text)
    scheduler.finish_materialization(
        job_id,
        source_path="/tmp/source.cpp",
        work_root="/tmp/work",
        success=True,
        now_text=now_text,
    )
    scheduler.record_compile_result(
        job_id,
        compile_success=1,
        compile_output_b64="",
        compile_metadata_b64="",
        lease_owner="host-setup",
        updated_at=now_text,
    )
    return job_id


class TestJudgehostScheduler(unittest.TestCase):
    @staticmethod
    def _testcase_toolkit(temp_root: Path) -> tuple[DomjudgeToolkit, JudgeFsIndexService]:
        cache = JudgeFsIndexService(temp_root / "index")
        state = SimpleNamespace(
            fs_manager=SimpleNamespace(judgehost_executables_root=temp_root / "executables"),
            judge_fs_index_service=cache,
        )
        return (DomjudgeToolkit(state), cache)

    @staticmethod
    def _register_testcase(
        toolkit: DomjudgeToolkit,
        input_blob: bytes,
        answer_blob: bytes,
    ) -> tuple[str, str]:
        input_hash = sha256_hex_bytes(input_blob)
        answer_hash = sha256_hex_bytes(answer_blob)
        signature = domjudge_hash_of_hashes([input_hash, answer_hash])
        return toolkit.register_cached_testcase(
            testcase_hash=signature,
            testcase_signature=signature,
            input_hash=input_hash,
            answer_hash=answer_hash,
            in_bytes=input_blob,
            ans_bytes=answer_blob,
        )

    def test_task_registry_has_identity_but_no_scheduler(self) -> None:
        registry = JudgehostTaskRegistry()
        registry.insert(_task_row(1))
        self.assertEqual(registry.get_for_run("run-1")["id"], "task-1")
        self.assertFalse(hasattr(registry, "claim_ready"))
        self.assertFalse(hasattr(registry, "renew"))

    def test_invalid_case_spec_does_not_partially_create_job(self) -> None:
        scheduler = JobScheduler(id_base=50)
        invalid_case = _case_row("invalid", "run-invalid", 1, 1)
        invalid_case.pop("answer_ref")
        with self.assertRaisesRegex(RuntimeError, "missing answer_ref"):
            _create_staged_job(
                scheduler,
                task_id="invalid",
                run_id="run-invalid",
                ordinals=[1],
                case_rows=[invalid_case],
            )
        self.assertIsNone(scheduler.fetch_job(51))

        with patch("app.service.judgehost.job_scheduler.domjudge_script_id", side_effect=RuntimeError("invalid script hash")):
            with self.assertRaisesRegex(RuntimeError, "invalid script hash"):
                _create_staged_job(
                    scheduler,
                    task_id="invalid-script",
                    run_id="run-invalid-script",
                    ordinals=[1],
                )
        self.assertIsNone(scheduler.fetch_job(51))

    def test_ready_job_prefers_foreground_then_numeric_test_order(self) -> None:
        scheduler = JobScheduler(id_base=100)
        background_id = _create_ready_job(
            scheduler,
            task_id="background",
            run_id="run-background",
            ordinals=[10, 2, 1],
        )
        foreground_id = _create_ready_job(
            scheduler,
            task_id="foreground",
            run_id="run-foreground",
            ordinals=[3],
            service_class="foreground",
            scope=2,
        )
        self.assertEqual(scheduler.claim_ready_job("host-a")["job_id"], foreground_id)
        foreground = scheduler.lease_cases(
            foreground_id,
            hostname="host-a",
            limit=2,
            now_text=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual([row["ordinal"] for row in foreground], [3])
        self.assertEqual(scheduler.claim_ready_job("host-b")["job_id"], background_id)
        background = scheduler.lease_cases(
            background_id,
            hostname="host-b",
            limit=3,
            now_text=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual([row["ordinal"] for row in background], [1, 2, 10])

    def test_job_and_case_ids_share_one_collision_free_namespace(self) -> None:
        scheduler = JobScheduler(id_base=150)
        job_id, _now_text = _create_staged_job(
            scheduler,
            task_id="identity",
            run_id="run-identity",
            ordinals=[1],
        )
        case_id = int(scheduler.cases_for_job(job_id)[0]["id"])

        self.assertEqual(job_id, 151)
        self.assertEqual(case_id, 152)

    def test_ready_job_claim_does_not_consume_untransitioned_work(self) -> None:
        scheduler = JobScheduler(id_base=200)
        job_id = _create_ready_job(
            scheduler,
            task_id="retryable",
            run_id="run-retryable",
            ordinals=[1],
        )

        self.assertEqual(scheduler.claim_ready_job("host-a")["job_id"], job_id)
        self.assertEqual(scheduler.claim_ready_job("host-b")["job_id"], job_id)

    def test_bulk_case_transitions_refresh_job_heap_once(self) -> None:
        scheduler = JobScheduler(id_base=300)
        job_id, now_text = _create_staged_job(
            scheduler,
            task_id="bulk",
            run_id="run-bulk",
            ordinals=list(range(1, 257)),
        )

        self.assertTrue(scheduler.activate_task_cases("bulk", now_text=now_text))
        self.assertEqual(len(scheduler._ready_jobs), 1)
        cache_rows = scheduler.cache_pending_cases(job_id)
        retry_rows = scheduler.cache_pending_cases(job_id)
        self.assertEqual(
            [row["id"] for row in retry_rows],
            [row["id"] for row in cache_rows],
        )
        scheduler.mark_cache_misses(
            [int(row["id"]) for row in cache_rows],
            now_text=now_text,
        )
        self.assertLessEqual(len(scheduler._ready_jobs), 2)

    def test_script_hash_index_tracks_open_job_references(self) -> None:
        scheduler = JobScheduler(id_base=400)
        first_job, now_text = _create_staged_job(
            scheduler,
            task_id="first-script-user",
            run_id="run-first-script-user",
            ordinals=[1],
        )
        second_job, _ = _create_staged_job(
            scheduler,
            task_id="second-script-user",
            run_id="run-second-script-user",
            ordinals=[1],
        )
        compile_hash = "1" * 32
        compile_id = int(domjudge_script_id(compile_hash))

        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), {compile_hash})
        self.assertIsNotNone(
            scheduler.claim_job_finalization(
                first_job,
                now_text=now_text,
                force_runresult="internal-error",
            )
        )
        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), {compile_hash})
        self.assertIsNotNone(
            scheduler.claim_job_finalization(
                second_job,
                now_text=now_text,
                force_runresult="internal-error",
            )
        )
        self.assertEqual(scheduler.active_script_hashes("compile", compile_id), set())

    def test_sixteen_hosts_never_lease_a_case_twice(self) -> None:
        scheduler = JobScheduler(id_base=1000)
        job_id = _create_ready_job(
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
            job = scheduler.claim_ready_job(hostname)
            if job is None:
                return
            rows = scheduler.lease_cases(
                int(job["job_id"]),
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
        self.assertEqual(len(scheduler.cases_for_job(job_id, status="leased")), 256)

    def test_judgehost_runtime_defaults_and_overrides(self) -> None:
        defaults = build_runtime_values()
        self.assertEqual(defaults.JUDGEHOST_FETCH_BATCH_SIZE, 2)
        self.assertEqual(defaults.RUN_EXEC_PROCESS_LIMIT, 1024)
        overridden = build_runtime_values(
            {"JUDGEHOST_FETCH_BATCH_SIZE": 8, "RUN_EXEC_PROCESS_LIMIT": 256}
        )
        self.assertEqual(overridden.JUDGEHOST_FETCH_BATCH_SIZE, 8)
        self.assertEqual(overridden.RUN_EXEC_PROCESS_LIMIT, 256)

    def test_case_cache_metadata_hit_does_not_read_payload_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="judge-fs-index-") as temp_dir:
            cache = JudgeFsIndexService(Path(temp_dir))
            cache.put(
                kind=JudgeFsIndexService.KIND_CASE,
                key_hash="1" * 64,
                signature="2" * 64,
                value={"manifest": []},
                files={"program.out": b"large output"},
            )
            with patch.object(Path, "read_bytes", side_effect=AssertionError("payload was read")):
                entry = cache.get(
                    kind=JudgeFsIndexService.KIND_CASE,
                    key_hash="1" * 64,
                    signature="2" * 64,
                )
            self.assertIsNotNone(entry)

    def test_testcase_registration_hits_without_rewrite_and_recovers_truncation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="judge-testcase-cache-") as temp_dir:
            toolkit, cache = self._testcase_toolkit(Path(temp_dir))
            input_blob = b"input\n"
            answer_blob = b"answer\n"
            first_refs = self._register_testcase(toolkit, input_blob, answer_blob)
            with (
                patch.object(cache, "put", side_effect=AssertionError("cache hit rewrote testcase")),
                patch(
                    "app.service.judgehost.toolkit.sha256_hex_bytes",
                    side_effect=AssertionError("payload rehashed"),
                ),
            ):
                self.assertEqual(self._register_testcase(toolkit, input_blob, answer_blob), first_refs)
            self.assertNotEqual(
                self._register_testcase(toolkit, input_blob, b"other answer\n"),
                first_refs,
            )

    def test_concurrent_testcase_registration_writes_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="judge-testcase-concurrent-") as temp_dir:
            toolkit, cache = self._testcase_toolkit(Path(temp_dir))
            barrier = threading.Barrier(8)
            refs: list[tuple[str, str]] = []

            def _register() -> None:
                barrier.wait(timeout=5)
                refs.append(self._register_testcase(toolkit, b"input\n", b"answer\n"))

            with patch.object(cache, "put", wraps=cache.put) as cache_put:
                threads = [threading.Thread(target=_register) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(set(refs)), 1)
            self.assertEqual(cache_put.call_count, 1)

    def test_terminal_cleanup_removes_runtime_identity_but_not_cache(self) -> None:
        registry = JudgehostTaskRegistry()
        row = _task_row(1, verification_id="verification-cleanup")
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
        cleanup._generation_by_verification["verification-cleanup"] = 2
        self.assertTrue(cleanup._cleanup("verification-cleanup", expected_generation=2))
        self.assertEqual(cases.forgotten_runs, ["run-1"])
        self.assertEqual(cases.forgotten_scopes, ["verification-cleanup"])
        self.assertIsNone(registry.get("task-1"))


if __name__ == "__main__":
    unittest.main()
