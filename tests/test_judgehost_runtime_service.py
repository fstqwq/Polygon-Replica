from __future__ import annotations

import hashlib
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.api import Judgehost
from app.service.judgehost.batch_scheduler_models import (
    CompileSubmission,
    ExecutionBatchSpec,
)
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.identity import domjudge_submit_id
from app.service.judgehost.toolchain_versions import HostToolchainTelemetry
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.judgehost_adapter import VerificationJudgehostAdapter
from tests.db_fixture import DBTestBase


_NOW = "2026-08-11T00:00:00+00:00"
_PROGRAM_ID = "solution-0"


class TestJudgehostRuntimeService(DBTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.completion_service = VerificationTaskCompletionService(
            self.verification_task_store,
            self.runtime_blob_store,
            lambda _verification_id, _commit: False,
        )
        self.runtime_registry = VerificationRuntimeRegistry()
        self.service = Judgehost(
            self.workspace_service,
            self.config_values,
            execution_port=VerificationJudgehostAdapter(
                self.db,
                self.verification_task_store,
                self.completion_service,
                self.runtime_registry,
            ),
            runtime_blob_store=self.runtime_blob_store,
            runtime_cache_index=self.runtime_cache_index,
        )
        self.config_values.replace(
            {
                **self.config_values.snapshot(),
                "JUDGEHOST_ENABLE": True,
                "JUDGEHOST_API_TOKEN": "test-token",
                "JUDGEHOST_API_USERNAME": "judgehost",
            }
        )
        self.addCleanup(self.service.reset_runtime_state)

    def _seed_workspace(self) -> None:
        self.workspace_service.ensure_problem(self.problem)
        self.workspace_service.grant_repo_access(
            self.problem,
            self.user,
            "owner",
        )
        self.workspace_service.ensure_workspace(
            self.problem,
            self.user,
            refresh_status=False,
        )

    def _insert_task(
        self,
        *,
        task_id: str,
        run_id: str,
        verification_id: str,
        status: str,
        run_status: str = "",
        error_text: str = "",
    ) -> None:
        self.service.state.task_registry.insert(
            {
                "id": task_id,
                "run_id": run_id,
                "problem_slug": self.problem,
                "username": self.user,
                "artifact_verification_id": verification_id,
                "verification_id": verification_id,
                "verification_task_id": "",
                "mode": "pass-fail",
                "status": status,
                "payload": {
                    "task_kind": "solution-run",
                    "verification_source": "run.execute",
                    "source_path": "solutions/std.cpp",
                },
                "result": {},
                "persist_verification_run": False,
                "run_status": run_status,
                "error_text": error_text,
                "created_at": _NOW,
                "updated_at": _NOW,
                "completed_at": _NOW if run_status else "",
                "summary": {
                    "mode": "pass-fail",
                    "source": "solutions/std.cpp",
                    "tests": [],
                    "compile_diagnostics": [],
                    "error": error_text,
                    "status": run_status,
                },
                "enqueue_fingerprint": "",
            }
        )

    @staticmethod
    def _compile_submission(compile_key: str) -> CompileSubmission:
        content = b"int main() { return 0; }\n"
        return CompileSubmission(
            compile_key=compile_key,
            submit_id=domjudge_submit_id(compile_key),
            source_name="std.cpp",
            source_file=PayloadFile(
                path=Path("/tmp/test-judgehost-runtime-service.cpp"),
                size=len(content),
                identity=hashlib.sha256(content).hexdigest(),
            ),
            extra_source_items=(),
            compile_files=(),
        )

    def test_precompute_rejects_illegal_internal_memory_limit(self) -> None:
        source = self.runtime_blob_store.put_bytes(b"int main(){return 0;}\n")
        verification_payload = {
            "run_config_json": '{"memory_limit_mb":0}',
            "problem_limits": {
                "time_limit_ms": 2000,
                "memory_limit_mb": 1,
                "pass_limit": 1,
            },
            "source_files": {},
        }
        with self.assertRaisesRegex(
            ValueError,
            "invalid internal memory_limit_mb",
        ):
            self.service.prepare_execution_template(
                mode="pass-fail",
                upload_file=source,
                upload_filename="solution.cpp",
                verification_payload=verification_payload,
                expected_behavior="accepted",
                verification_source="verification",
                task_kind="solution-run",
            )

    def test_set_host_enabled_preserves_status_shape(self) -> None:
        self.service.domjudge_register_host("judgehost-shape-check")
        before_host = next(
            item
            for item in self.service.status()["hosts"]
            if item["hostname"] == "judgehost-shape-check"
        )
        self.assertEqual(before_host["judged_case_count"], 0)
        self.assertIsNone(before_host["last_judging_at"])
        self.assertIsNone(before_host["recent_avg_per_case_sec"])
        self.assertNotIn("load_5m", before_host)

        release = self.service.set_host_enabled("judgehost-shape-check", False)
        self.assertIsInstance(release, dict)
        after_host = next(
            item
            for item in self.service.status()["hosts"]
            if item["hostname"] == "judgehost-shape-check"
        )
        self.assertFalse(after_host["enabled"])

    def test_host_status_keeps_latest_peer_ip(self) -> None:
        host = "judgehost-peer-display"
        self.service.domjudge_register_host(host)
        self.service.record_host_peer_addr(host, "203.0.113.10")
        before = next(
            item for item in self.service.status()["hosts"]
            if item["hostname"] == host
        )
        self.assertEqual(before["peer_addr"], "203.0.113.10")

        self.service.set_host_enabled(host, False)
        after = next(
            item for item in self.service.status()["hosts"]
            if item["hostname"] == host
        )
        self.assertEqual(after["peer_addr"], "203.0.113.10")

    def test_host_status_exposes_toolchains_and_reset_clears_them(self) -> None:
        host = "judgehost-toolchains"
        self.service.domjudge_register_host(host)
        with self.service.state.state_lock:
            self.service.state.host_toolchains[host] = {
                "py": HostToolchainTelemetry(
                    language_id="py",
                    compiler="command=/usr/bin/pypy3\nPython 3.10.16",
                    runner="command=/usr/bin/pypy3\nPython 3.10.16",
                    observed_at="2026-08-08T00:00:00+00:00",
                    judgetask_id=102,
                ),
                "cpp": HostToolchainTelemetry(
                    language_id="cpp",
                    compiler="command=/usr/bin/g++\ng++ 14.2.0",
                    runner="",
                    observed_at="2026-08-08T00:00:01+00:00",
                    judgetask_id=101,
                ),
            }

        row = next(
            item for item in self.service.status()["hosts"]
            if item["hostname"] == host
        )
        self.assertEqual(
            [item["language_id"] for item in row["toolchains"]],
            ["cpp", "py"],
        )
        self.service.set_host_enabled(host, False)
        disabled = next(
            item for item in self.service.status()["hosts"]
            if item["hostname"] == host
        )
        self.assertEqual(disabled["toolchains"], row["toolchains"])

        self.service.reset_runtime_state()
        self.assertEqual(self.service.state.host_toolchains, {})

    def test_expired_lease_reconcile_reads_online_window_from_policy(self) -> None:
        host = "judgehost-stale-policy"
        self.config_values.replace(
            {
                **self.config_values.snapshot(),
                "JUDGEHOST_ONLINE_WINDOW_SEC": 5,
            }
        )
        self.service.domjudge_register_host(host)
        with self.service.state.state_lock:
            self.service.state.hosts_state[host]["last_seen_at"] = (
                "2000-01-01T00:00:00+00:00"
            )

        with patch.object(
            self.service.state.batch_scheduler,
            "cases_for_host",
            return_value=[],
        ) as cases_for_host:
            released = self.service.reconcile_expired_verification_leases(
                "ver-policy-window"
            )

        self.assertEqual(released, [])
        cases_for_host.assert_called_once_with(host)

    def test_wait_for_transient_result_has_no_durable_artifact_path(self) -> None:
        verification_id = "ver-123"
        run_id = "run-transient"
        task_id = "task-transient"
        self._insert_task(
            task_id=task_id,
            run_id=run_id,
            verification_id=verification_id,
            status=self.service.STATUS_COMPLETED,
            run_status="ok",
        )

        result = self.service.wait_for_task_result(task_id, timeout_sec=1.0)
        self.assertEqual(result["artifact_path"], "")
        run_root = (
            self.storage_layout.resolve_verification_root(verification_id)
            / "runs"
            / run_id
        )
        self.assertFalse(run_root.exists())

    def test_prepare_java_payload_uses_detected_entry_point(self) -> None:
        self._seed_workspace()
        payload = self.service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id="",
            mode="pass-fail",
            submission_path=None,
            upload_content=(
                b"public class TranslateMain {\n"
                b"  public static void main(String[] args) {}\n"
                b"}\n"
            ),
            upload_filename="java_translate.java",
            run_id=f"r-java-entry-{uuid.uuid4().hex[:8]}",
            selected_tests=[],
            verification_id="ver-124",
            verification_program_id=_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        self.assertEqual(payload["source_name"], "TranslateMain.java")
        self.assertEqual(payload["entry_point"], "TranslateMain")
        precomputed = dict(payload["domjudge_precomputed"])
        run_config = dict(precomputed["run_config"])
        self.assertEqual(run_config["entry_point"], "TranslateMain")

    def test_prepare_java_payload_rejects_missing_main_class(self) -> None:
        self._seed_workspace()
        with self.assertRaisesRegex(RuntimeError, "no runnable main class found"):
            self.service.prepare_enqueue_payload(
                problem=self.problem,
                username=self.user,
                artifact_verification_id="",
                mode="pass-fail",
                submission_path=None,
                upload_content=b"class Helper {}\n",
                upload_filename="helper.java",
                run_id=f"r-java-missing-main-{uuid.uuid4().hex[:8]}",
                selected_tests=[],
                verification_id="ver-125",
                verification_program_id=_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
            )

    def test_poll_result_reports_missing_case_explicitly(self) -> None:
        self._insert_task(
            task_id="task-missing",
            run_id="run-missing",
            verification_id="ver-126",
            status=self.service.STATUS_FAILED,
            run_status="failed",
        )
        result = self.service.poll_task_case_result("task-missing", "001.in")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["missing_case_result"])
        self.assertIn("missing for 001.in", result["error"])
        self.assertEqual(result["summary"]["tests"], [])

    def test_poll_result_uses_canonical_case_feedback(self) -> None:
        task_id = "task-feedback"
        run_id = "run-feedback"
        verification_id = "ver-127"
        self._insert_task(
            task_id=task_id,
            run_id=run_id,
            verification_id=verification_id,
            status=self.service.STATUS_LEASED,
        )
        compile_key = "8" * 64
        batch_id = self.service.state.batch_scheduler.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=_PROGRAM_ID,
            execution_signature="b" * 64,
            task_kind="solution-run",
            verification_id=verification_id,
            compile_key=compile_key,
            compile_submission=self._compile_submission(compile_key),
            contest_id="",
            mode="pass-fail",
            source_name="std.cpp",
            compile_hash="a" * 32,
            run_hash="b" * 32,
            compare_hash="c" * 32,
            source_hash="d" * 64,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source="run.execute",
            bypass_case_result_cache=0,
            service_class="background",
            batch_spec=ExecutionBatchSpec(),
            created_at=_NOW,
            case_rows=[
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "test_name": "016.in",
                    "ordinal": 1,
                    "testcase_id": 16,
                    "testcase_hash": "e" * 64,
                    "testcase_input_hash": "f" * 64,
                    "testcase_answer_hash": "0" * 64,
                    "input_ref": "",
                    "answer_ref": "",
                    "status": "pending",
                }
            ],
        )
        scheduler = self.service.state.batch_scheduler
        self.assertTrue(scheduler.claim_materialization(batch_id, now_text=_NOW))
        self.assertTrue(
            scheduler.finish_materialization(
                batch_id,
                success=True,
                error_text="",
                now_text=_NOW,
            )
        )
        case = scheduler.lease_cases(
            batch_id,
            hostname="judgehost-feedback",
            limit=1,
            now_text=_NOW,
        )[0]
        case_id = int(case["id"])
        receipt = scheduler.acquire_case_callback_receipt(case_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        scheduler.release_case_callback_receipt(receipt.receipt_id)
        claim = scheduler.claim_case_reporting(
            case_id,
            hostname="judgehost-feedback",
            receipt_generation=receipt.claim_generation,
            now_text=_NOW,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        feedback_text = "Unexpected character #10, but ' ' expected (testdata.in)"
        artifact_ref = "blob://sha256/" + "b" * 64
        result = build_case_result(
            test_name="016.in",
            runresult="checker-fail",
            verdict="FL",
            runtime_sec=0.012,
            cpu_sec=0.011,
            wall_sec=0.025,
            memory_kb=1404,
            score_text="",
            output_run_ref=artifact_ref,
            output_error_ref=artifact_ref,
            output_system_ref=artifact_ref,
            output_diff_ref=artifact_ref,
            metadata_ref=artifact_ref,
            compare_metadata_ref=artifact_ref,
            team_message_ref=artifact_ref,
            feedback_text=feedback_text,
            feedback_files=["feedback/judgemessage.txt"],
            answer_correct=False,
            input_ref=artifact_ref,
        )
        self.assertEqual(
            scheduler.commit_case_result(
                case_id,
                generation=claim.generation,
                result=result,
                updated_at=_NOW,
            ),
            "reported",
        )

        report = self.service.poll_task_case_result(task_id, "016.in")
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"], feedback_text)
        self.assertEqual(report["summary"]["error"], feedback_text)
        tests = list(report["summary"]["tests"])
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0]["message"], feedback_text)
        self.assertEqual(tests[0]["feedback_files"], [artifact_ref])


if __name__ == "__main__":
    unittest.main()
