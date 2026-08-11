from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.batch_scheduler_models import (
    CaseReportTelemetry,
    CompileSubmission,
    ExecutionBatchSpec,
)
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.identity import domjudge_submit_id
from app.service.judgehost.toolchain_versions import (
    ToolchainTelemetryHandler,
    ToolchainVersionCollector,
    ToolchainVersionReport,
)
from app.service.platform.hashing import compile_command_digest
from app.service.platform.runtime_blob_store import PayloadFile


_NOW = "2026-08-03T01:00:00+00:00"
_HASH = "1" * 64
_COMPILE_KEY = "5" * 64


class _VersionScheduler:
    def __init__(self) -> None:
        self.case: dict[str, object] | None = None
        self.batch: dict[str, object] | None = None

    def fetch_case(self, case_id: int) -> dict[str, object] | None:
        _ = case_id
        return self.case

    def fetch_batch(self, batch_id: int) -> dict[str, object] | None:
        _ = batch_id
        return self.batch


class TestToolchainVersionCollector(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = _VersionScheduler()
        self.state = SimpleNamespace(
            config_values=SimpleNamespace(
                TOOLCHAIN_CPP_COMPILER="/opt/tool chains/clang++",
                TOOLCHAIN_JAVA_COMPILER="javac-custom",
            ),
            batch_scheduler=self.scheduler,
            state_lock=threading.RLock(),
            host_toolchains={},
        )
        self.collector = ToolchainVersionCollector(self.state)
        self._lease("cpp")

    def _lease(
        self,
        language_id: str,
        *,
        case_id: int = 101,
        hostname: str = "judgehost-a",
        status: str = "leased",
        toolchain_cmd_digest: str = "a" * 64,
    ) -> None:
        self.scheduler.case = {
            "id": case_id,
            "batch_id": 11,
            "status": status,
            "lease_owner": hostname,
        }
        self.scheduler.batch = {
            "batch_id": 11,
            "compile_config_json": json.dumps(
                {"toolchain_cmd_digest": toolchain_cmd_digest}
            ),
            "run_config_json": json.dumps({"language_id": language_id}),
        }

    @staticmethod
    def _encoded(payload: bytes) -> str:
        return base64.b64encode(payload).decode("ascii")

    def test_version_commands_match_actual_language_toolchains(self) -> None:
        cpp = self.collector.version_commands(101)
        cpp_script = str(cpp["compiler_version_command"])
        self.assertNotIn("runner_version_command", cpp)
        self.assertIn("command -v '/opt/tool chains/clang++'", cpp_script)
        self.assertIn("exec '/opt/tool chains/clang++' --version 2>&1", cpp_script)

        self._lease("c")
        self.assertEqual(self.collector.version_commands(101), cpp)

        self._lease("java")
        java = self.collector.version_commands(101)
        self.assertIn("exec javac-custom -version 2>&1", str(java["compiler_version_command"]))
        self.assertIn("exec java -version 2>&1", str(java["runner_version_command"]))

        self._lease("py")
        python = self.collector.version_commands(101)
        compiler_script = str(python["compiler_version_command"])
        self.assertEqual(python["runner_version_command"], compiler_script)
        self.assertLess(compiler_script.index("pypy3"), compiler_script.index("python3"))
        self.assertLess(compiler_script.index("python3"), compiler_script.index("python;"))

    def test_version_commands_require_an_active_non_skip_lease(self) -> None:
        self.scheduler.case = None
        self.assertEqual(self.collector.version_commands(101), {})

        self._lease("cpp", status="reported")
        self.assertEqual(self.collector.version_commands(101), {})

        self._lease(
            "cpp",
            toolchain_cmd_digest=compile_command_digest("skip.compile", []),
        )
        self.assertEqual(self.collector.version_commands(101), {})

    def test_report_decodes_and_canonicalizes_version_output(self) -> None:
        self.assertTrue(
            self.collector.record_report(
                101,
                hostname="judgehost-a",
                compiler=self._encoded(b" command=/usr/bin/g++\r\ng++ 14\xff\x00 \r\n"),
                runner="",
            )
        )

        telemetry = self.state.host_toolchains["judgehost-a"]["cpp"]
        self.assertEqual(telemetry.language_id, "cpp")
        self.assertEqual(telemetry.compiler, "command=/usr/bin/g++\ng++ 14\ufffd\ufffd")
        self.assertEqual(telemetry.runner, "")
        self.assertEqual(telemetry.judgetask_id, 101)
        self.assertTrue(telemetry.observed_at)

    def test_report_requires_current_owner_and_valid_bounded_content(self) -> None:
        self.assertFalse(
            self.collector.record_report(
                101,
                hostname="judgehost-b",
                compiler=self._encoded(b"g++ 14"),
                runner="",
            )
        )
        self.assertEqual(self.state.host_toolchains, {})

        self.assertFalse(
            self.collector.record_report(
                101,
                hostname="judgehost-a",
                compiler="not base64",
                runner=self._encoded(b"x" * (ToolchainVersionCollector.MAX_VERSION_OUTPUT_BYTES + 1)),
            )
        )
        self.assertEqual(self.state.host_toolchains, {})

    def test_latest_language_report_overwrites_without_removing_other_languages(self) -> None:
        self.collector.record_report(
            101,
            hostname="judgehost-a",
            compiler=self._encoded(b"g++ 13"),
            runner="",
        )
        self.collector.record_report(
            101,
            hostname="judgehost-a",
            compiler=self._encoded(b"g++ 14"),
            runner="",
        )
        self._lease("java", case_id=102)
        self.collector.record_report(
            102,
            hostname="judgehost-a",
            compiler=self._encoded(b"javac 21"),
            runner=self._encoded(b"java 21"),
        )

        toolchains = self.state.host_toolchains["judgehost-a"]
        self.assertEqual(set(toolchains), {"cpp", "java"})
        self.assertEqual(toolchains["cpp"].compiler, "g++ 14")
        self.assertEqual(toolchains["java"].runner, "java 21")

    def test_handler_records_versions_event_after_telemetry(self) -> None:
        events: list[dict[str, object]] = []
        handler = ToolchainTelemetryHandler(
            self.state,
            lambda **event: events.append(event),
        )

        handler.record_report(
            ToolchainVersionReport(
                judgetask_id=101,
                hostname="judgehost-a",
                compiler=self._encoded(b"g++ 14"),
                runner="",
                task_id="task-101",
                run_id="run-101",
            )
        )

        self.assertEqual(
            events,
            [
                {
                    "hostname": "judgehost-a",
                    "action": "versions",
                    "task_id": "task-101",
                    "run_id": "run-101",
                }
            ],
        )

    def test_handler_contains_optional_telemetry_failures(self) -> None:
        events: list[dict[str, object]] = []
        handler = ToolchainTelemetryHandler(
            self.state,
            lambda **event: events.append(event),
        )
        assert self.scheduler.batch is not None
        self.scheduler.batch["compile_config_json"] = "not-json"

        self.assertEqual(handler.version_commands(101), {})
        handler.record_report(
            ToolchainVersionReport(
                judgetask_id=101,
                hostname="judgehost-a",
                compiler=self._encoded(b"g++ 14"),
                runner="",
                task_id="task-101",
                run_id="run-101",
            )
        )

        self.assertEqual(events, [])
        self.assertEqual(self.state.host_toolchains, {})

    def test_handler_contains_host_event_sink_failure(self) -> None:
        def failing_sink(
            *,
            hostname: str,
            action: str,
            task_id: str,
            run_id: str,
        ) -> None:
            _ = (hostname, action, task_id, run_id)
            raise RuntimeError("host event store unavailable")

        handler = ToolchainTelemetryHandler(self.state, failing_sink)

        handler.record_report(
            ToolchainVersionReport(
                judgetask_id=101,
                hostname="judgehost-a",
                compiler=self._encoded(b"g++ 14"),
                runner="",
                task_id="task-101",
                run_id="run-101",
            )
        )

        telemetry = self.state.host_toolchains["judgehost-a"]["cpp"]
        self.assertEqual(telemetry.compiler, "g++ 14")


def _result(test_name: str):
    return build_case_result(
        test_name=test_name,
        runresult="correct",
        verdict="OK",
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


class TestHostTelemetryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = BatchScheduler(id_base=100)
        self.sequence = 0

    def _batch(self, case_count: int) -> int:
        self.sequence += 1
        task_id = f"task-{self.sequence}"
        run_id = f"run-{self.sequence}"
        signature = hashlib.sha256(run_id.encode()).hexdigest()
        batch_id = self.scheduler.create_batch_with_cases(
            task_id=task_id,
            run_id=run_id,
            verification_program_id=f"solution-{self.sequence}",
            execution_signature=signature,
            task_kind="solution-run",
            verification_id="ver-1",
            compile_key=_COMPILE_KEY,
            compile_submission=CompileSubmission(
                compile_key=_COMPILE_KEY,
                submit_id=domjudge_submit_id(_COMPILE_KEY),
                source_name="ac.cpp",
                source_file=PayloadFile(
                    path=Path("/tmp/telemetry-ac.cpp"),
                    size=13,
                    identity=hashlib.sha256(b"int main(){}\n").hexdigest(),
                ),
                extra_source_items=(),
                compile_files=(),
            ),
            contest_id="default",
            mode="pass-fail",
            source_name="ac.cpp",
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
            case_rows=[
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "test_name": f"{index:03}.in",
                    "ordinal": index,
                    "scope_sequence": 1,
                    "testcase_id": None,
                    "testcase_hash": _HASH,
                    "testcase_input_hash": _HASH,
                    "testcase_answer_hash": _HASH,
                    "input_ref": "",
                    "answer_ref": "",
                    "status": "pending",
                }
                for index in range(1, case_count + 1)
            ],
        )
        self.assertTrue(self.scheduler.claim_materialization(batch_id, now_text=_NOW))
        self.assertTrue(
            self.scheduler.finish_materialization(
                batch_id,
                success=True,
                error_text="",
                now_text=_NOW,
            )
        )
        return batch_id

    def _report(self, hostname: str, case_id: int, at: float) -> None:
        row = self.scheduler.fetch_case(case_id)
        self.assertIsNotNone(row)
        receipt = self.scheduler.acquire_case_callback_receipt(case_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.scheduler.release_case_callback_receipt(receipt.receipt_id)
        claim = self.scheduler.claim_case_reporting(
            case_id,
            hostname=hostname,
            receipt_generation=receipt.claim_generation,
            now_text=_NOW,
        )
        self.assertIsNotNone(claim)
        report = CaseReportTelemetry(
            hostname=hostname,
            reported_at=f"2026-08-03T01:00:{int(at):02d}+00:00",
            reported_monotonic=at,
            verification_id="ver-1",
            problem_slug="alice/sample",
            task_kind="solution-run",
            source_label="ac.cpp",
            test_name=str(row["test_name"]),
        )
        outcome = self.scheduler.commit_case_result(
            case_id,
            generation=claim.generation,
            result=_result(str(row["test_name"])),
            updated_at=_NOW,
            report_telemetry=report,
        )
        self.assertEqual(outcome, "reported")

    def test_complete_fetch_batch_samples_immediately(self) -> None:
        batch_id = self._batch(2)
        rows = self.scheduler.lease_cases(batch_id, hostname="host-a", limit=2, now_text=_NOW)
        case_ids = [int(row["id"]) for row in rows]
        self.scheduler.record_batch_leased("host-a", batch_id, case_ids, leased_monotonic=10.0)
        self._report("host-a", case_ids[0], 11.0)
        self.assertIsNone(
            self.scheduler.host_telemetry_snapshot()["host-a"]["recent_avg_per_case_sec"]
        )
        self._report("host-a", case_ids[1], 14.0)

        row = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 2)
        self.assertEqual(row["recent_avg_per_case_sec"], 2.0)
        self.assertEqual(row["last_judging"]["test_name"], "002.in")

    def test_shared_batch_keeps_host_samples_independent(self) -> None:
        batch_id = self._batch(5)
        host_a = self.scheduler.lease_cases(batch_id, hostname="host-a", limit=2, now_text=_NOW)
        host_b = self.scheduler.lease_cases(batch_id, hostname="host-b", limit=3, now_text=_NOW)
        for hostname, rows, end in (("host-a", host_a, 4.0), ("host-b", host_b, 9.0)):
            case_ids = [int(row["id"]) for row in rows]
            self.scheduler.record_batch_leased(hostname, batch_id, case_ids, leased_monotonic=0.0)
            for case_id in case_ids:
                self._report(hostname, case_id, end)

        telemetry = self.scheduler.host_telemetry_snapshot()
        self.assertEqual(telemetry["host-a"]["recent_avg_per_case_sec"], 2.0)
        self.assertEqual(telemetry["host-b"]["recent_avg_per_case_sec"], 3.0)

    def test_median_uses_only_last_ten_fetch_batches(self) -> None:
        for duration in range(1, 12):
            batch_id = self._batch(1)
            row = self.scheduler.lease_cases(batch_id, hostname="host-a", limit=1, now_text=_NOW)[0]
            case_id = int(row["id"])
            self.scheduler.record_batch_leased("host-a", batch_id, [case_id], leased_monotonic=0.0)
            self._report("host-a", case_id, float(duration))

        row = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 11)
        self.assertEqual(row["recent_avg_per_case_sec"], 6.5)

    def test_host_release_discards_incomplete_fetch_batch(self) -> None:
        first_batch = self._batch(2)
        rows = self.scheduler.lease_cases(first_batch, hostname="host-a", limit=2, now_text=_NOW)
        case_ids = [int(row["id"]) for row in rows]
        self.scheduler.record_batch_leased("host-a", first_batch, case_ids, leased_monotonic=0.0)
        self._report("host-a", case_ids[0], 1.0)
        self.scheduler.release_host_leases("host-a", now_text=_NOW)

        second_batch = self._batch(1)
        row = self.scheduler.lease_cases(second_batch, hostname="host-a", limit=1, now_text=_NOW)[0]
        case_id = int(row["id"])
        self.scheduler.record_batch_leased("host-a", second_batch, [case_id], leased_monotonic=2.0)
        self._report("host-a", case_id, 5.0)

        telemetry = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(telemetry["judged_case_count"], 2)
        self.assertEqual(telemetry["recent_avg_per_case_sec"], 3.0)


if __name__ == "__main__":
    unittest.main()
