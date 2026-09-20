import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import build_config_values
from app.service.judgehost.batch.model import (
    CaseReportTelemetry,
    CompileSubmission,
    ExecutionBatchSpec,
    JudgehostCaseRow,
)
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.configuration import JudgehostConfiguration
from app.service.execution.policy import normalize_execution_result
from app.service.judgehost.domjudge.identity import submit_id
from app.service.judgehost.domjudge.wire import DomjudgeWireProjector
from app.service.judgehost.host.registry import JudgehostHostRegistry
from app.service.judgehost.host.status import JudgehostHostStatus
from app.service.judgehost.host.toolchain_versions import (
    ToolchainTelemetryHandler,
    ToolchainVersionCollector,
    ToolchainVersionReport,
)
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.platform.hashing import compile_command_digest
from app.service.platform.runtime_blob_store import PayloadFile

_NOW = "2026-08-03T01:00:00+00:00"
_HASH = "1" * 64
_COMPILE_KEY = "5" * 64


def _create_telemetry_batch(
    scheduler: JudgehostBatchRuntime,
    *,
    task_id: str,
    case_count: int,
    language_id: str = "cpp",
    toolchain_cmd_digest: str = "a" * 64,
) -> int:
    batch_id = scheduler.create_batch_with_cases(
        task_id=task_id,
        run_id=task_id,
        verification_program_id=task_id,
        execution_signature=hashlib.sha256(task_id.encode()).hexdigest(),
        task_kind="solution-run",
        verification_id="ver-1",
        compile_key=_COMPILE_KEY,
        compile_submission=CompileSubmission(
            compile_key=_COMPILE_KEY,
            submit_id=submit_id(_COMPILE_KEY),
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
        compile_config_json=json.dumps({"toolchain_cmd_digest": toolchain_cmd_digest}),
        run_config_json=json.dumps({"language_id": language_id}),
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
                "run_id": task_id,
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
    claim = scheduler.claim_materialization(batch_id, now_text=_NOW)
    assert claim is not None
    submission = replace(
        claim.submission,
        source_file=replace(
            claim.submission.source_file,
            blob_ref=f"blob://sha256/{claim.submission.source_file.identity}",
        ),
    )
    assert scheduler.finish_materialization(
        claim,
        success=True,
        materialized_submission=submission,
        error_text="",
        now_text=_NOW,
    )
    return batch_id


class TestToolchainVersionCollector(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = JudgehostBatchRuntime(id_base=100)
        self.config_values = build_config_values()
        self.config_values.replace(
            {
                **self.config_values.snapshot(),
                "TOOLCHAIN_CPP_COMPILER": "/opt/tool chains/clang++",
                "TOOLCHAIN_JAVA_COMPILER": "javac-custom",
            }
        )
        self.hosts = JudgehostHostRegistry()
        self.configuration = JudgehostConfiguration(self.config_values)
        self.collector = ToolchainVersionCollector(
            self.scheduler,
            self.configuration,
            self.hosts,
        )
        self._lease("cpp")

    def _lease(
        self,
        language_id: str,
        *,
        hostname: str = "judgehost-a",
        toolchain_cmd_digest: str = "a" * 64,
    ) -> None:
        self.scheduler.reset()
        batch_id = _create_telemetry_batch(
            self.scheduler, task_id="version-probe", case_count=1,
            language_id=language_id, toolchain_cmd_digest=toolchain_cmd_digest,
        )
        claim = self.scheduler.claim_lease(
            batch_id, hostname=hostname, limit=1, now_text=_NOW,
        )
        assert claim is not None
        assert self.scheduler.commit_lease(claim)
        self.case_id = claim.cases[0]["id"]

    @staticmethod
    def _encoded(payload: bytes) -> str:
        return base64.b64encode(payload).decode("ascii")

    def test_version_commands_match_actual_language_toolchains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool_dir = Path(temporary) / "tool chains"
            tool_dir.mkdir()
            for name in ("clang++", "javac-custom", "java", "pypy3", "python3", "python"):
                tool = tool_dir / name
                tool.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}' \"$@\"\n", encoding="utf-8")
                tool.chmod(0o700)
            self.config_values.replace({
                **self.config_values.snapshot(),
                "TOOLCHAIN_CPP_COMPILER": str(tool_dir / "clang++"),
            })

            def run(script: str) -> str:
                return subprocess.run(
                    ["/bin/sh"], input=script, text=True, capture_output=True,
                    env={"PATH": str(tool_dir)}, check=True, timeout=5,
                ).stdout

            cpp = self.collector.version_commands(self.case_id)
            self.assertNotIn("runner_version_command", cpp)
            self.assertEqual(
                run(cpp["compiler_version_command"]),
                f"command={tool_dir / 'clang++'}\nclang++\n--version\n",
            )

            self._lease("java")
            java = self.collector.version_commands(self.case_id)
            for script, name in ((java["compiler_version_command"], "javac-custom"),
                                 (java["runner_version_command"], "java")):
                self.assertEqual(
                    run(script), f"command={tool_dir / name}\n{name}\n-version\n",
                )

            self._lease("py")
            python = self.collector.version_commands(self.case_id)
            for name in ("pypy3", "python3", "python"):
                with self.subTest(python=name):
                    for script in python.values():
                        self.assertEqual(
                            run(script), f"command={tool_dir / name}\n{name}\n--version\n",
                        )
                    (tool_dir / name).unlink()

    def test_version_commands_reject_unknown_toolchain_language(self) -> None:
        self._lease("unsupported-language")

        with self.assertRaisesRegex(
            RuntimeError,
            "unsupported judgehost toolchain language: unsupported-language",
        ):
            self.collector.version_commands(self.case_id)

    def test_version_commands_require_an_active_non_skip_lease(self) -> None:
        self.assertEqual(self.collector.version_commands(-1), {})

        self.scheduler.release_host_leases("judgehost-a", now_text=_NOW)
        self.assertEqual(self.collector.version_commands(self.case_id), {})

        self._lease(
            "cpp",
            toolchain_cmd_digest=compile_command_digest("skip.compile", []),
        )
        self.assertEqual(self.collector.version_commands(self.case_id), {})

    def test_report_decodes_and_canonicalizes_version_output(self) -> None:
        self.assertTrue(
            self.collector.record_report(
                self.case_id,
                hostname="judgehost-a",
                compiler=self._encoded(b" command=/usr/bin/g++\r\ng++ 14\xff\x00 \r\n"),
                runner="",
            )
        )

        telemetry = self.hosts.toolchain_rows()["judgehost-a"]["cpp"]
        self.assertEqual(telemetry.language_id, "cpp")
        self.assertEqual(telemetry.compiler, "command=/usr/bin/g++\ng++ 14\ufffd\ufffd")
        self.assertEqual(telemetry.runner, "")
        self.assertEqual(telemetry.judgetask_id, self.case_id)
        self.assertTrue(telemetry.observed_at)

    def test_report_requires_current_owner_and_valid_bounded_content(self) -> None:
        self.assertFalse(
            self.collector.record_report(
                self.case_id,
                hostname="judgehost-b",
                compiler=self._encoded(b"g++ 14"),
                runner="",
            )
        )
        self.assertEqual(self.hosts.toolchain_rows(), {})

        self.assertFalse(
            self.collector.record_report(
                self.case_id,
                hostname="judgehost-a",
                compiler="not base64",
                runner=self._encoded(
                    b"x" * (ToolchainVersionCollector.MAX_VERSION_OUTPUT_BYTES + 1)
                ),
            )
        )
        self.assertEqual(self.hosts.toolchain_rows(), {})

    def test_latest_language_report_overwrites_without_removing_other_languages(
        self,
    ) -> None:
        self.collector.record_report(
            self.case_id,
            hostname="judgehost-a",
            compiler=self._encoded(b"g++ 13"),
            runner="",
        )
        self.collector.record_report(
            self.case_id,
            hostname="judgehost-a",
            compiler=self._encoded(b"g++ 14"),
            runner="",
        )
        self._lease("java")
        self.collector.record_report(
            self.case_id,
            hostname="judgehost-a",
            compiler=self._encoded(b"javac 21"),
            runner=self._encoded(b"java 21"),
        )

        toolchains = self.hosts.toolchain_rows()["judgehost-a"]
        self.assertEqual(set(toolchains), {"cpp", "java"})
        self.assertEqual(toolchains["cpp"].compiler, "g++ 14")
        self.assertEqual(toolchains["java"].runner, "java 21")

    def test_handler_records_host_contact_after_telemetry(self) -> None:
        handler = ToolchainTelemetryHandler(
            self.scheduler,
            self.configuration,
            self.hosts,
        )

        handler.record_report(
            ToolchainVersionReport(
                judgetask_id=self.case_id,
                hostname="judgehost-a",
                compiler=self._encoded(b"g++ 14"),
                runner="",
            )
        )

        host = self.hosts.host_rows()[0]
        self.assertEqual(host["hostname"], "judgehost-a")
        self.assertTrue(host["first_seen_at"])
        self.assertEqual(host["last_seen_at"], host["first_seen_at"])

    def test_handler_contains_optional_telemetry_failures(self) -> None:
        handler = ToolchainTelemetryHandler(
            self.scheduler,
            self.configuration,
            self.hosts,
        )
        self._lease("unsupported-language")

        self.assertEqual(handler.version_commands(self.case_id), {})
        handler.record_report(
            ToolchainVersionReport(
                judgetask_id=self.case_id,
                hostname="judgehost-a",
                compiler=self._encoded(b"g++ 14"),
                runner="",
            )
        )

        self.assertEqual(self.hosts.host_rows(), [])
        self.assertEqual(self.hosts.toolchain_rows(), {})

    def test_handler_keeps_telemetry_when_host_contact_clock_fails(self) -> None:
        handler = ToolchainTelemetryHandler(
            self.scheduler,
            self.configuration,
            self.hosts,
        )

        with patch(
            "app.service.judgehost.host.registry.now_iso",
            side_effect=RuntimeError("host contact clock unavailable"),
        ):
            handler.record_report(
                ToolchainVersionReport(
                    judgetask_id=self.case_id,
                    hostname="judgehost-a",
                    compiler=self._encoded(b"g++ 14"),
                    runner="",
                )
            )

        telemetry = self.hosts.toolchain_rows()["judgehost-a"]["cpp"]
        self.assertEqual(telemetry.compiler, "g++ 14")


class TestHostStatus(unittest.TestCase):
    def test_host_projections_are_sorted_by_name(self) -> None:
        hosts = JudgehostHostRegistry()
        for hostname in ("judgehost-zeta", "judgehost-alpha", "judgehost-beta"):
            hosts.record_contact(hostname=hostname)
        status = JudgehostHostStatus(
            hosts,
            JudgehostTaskRegistry(),
            JudgehostBatchRuntime(id_base=500),
        ).status(JudgehostConfiguration(build_config_values()).snapshot())
        rows = status["hosts"]

        self.assertEqual(
            [row["hostname"] for row in rows],
            ["judgehost-alpha", "judgehost-beta", "judgehost-zeta"],
        )
        self.assertEqual(
            [row["hostname"] for row in DomjudgeWireProjector.hosts(hosts.host_rows())],
            ["judgehost-alpha", "judgehost-beta", "judgehost-zeta"],
        )



class TestHostTelemetryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = JudgehostBatchRuntime(id_base=100)
        self.sequence = 0

    def _batch(self, case_count: int) -> int:
        self.sequence += 1
        return _create_telemetry_batch(
            self.scheduler,
            task_id=f"task-{self.sequence}",
            case_count=case_count,
        )

    def _lease_cases(
        self,
        batch_id: int,
        *,
        hostname: str,
        limit: int,
    ) -> list[JudgehostCaseRow]:
        claim = self.scheduler.claim_lease(
            batch_id,
            hostname=hostname,
            limit=limit,
            now_text=_NOW,
        )
        if claim is None:
            return []
        self.assertTrue(self.scheduler.commit_lease(claim))
        return list(claim.cases)

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
            result=normalize_execution_result(verdict="OK"),
            updated_at=_NOW,
            report_telemetry=report,
        )
        self.assertEqual(outcome, "reported")

    def test_complete_fetch_batch_samples_immediately(self) -> None:
        batch_id = self._batch(2)
        rows = self._lease_cases(batch_id, hostname="host-a", limit=2)
        case_ids = [int(row["id"]) for row in rows]
        self.scheduler.record_batch_leased(
            "host-a", batch_id, case_ids, leased_monotonic=10.0
        )
        self._report("host-a", case_ids[0], 11.0)
        self.assertIsNone(
            self.scheduler.host_telemetry_snapshot()["host-a"][
                "recent_avg_per_case_sec"
            ]
        )
        self._report("host-a", case_ids[1], 14.0)

        row = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 2)
        self.assertEqual(row["recent_avg_per_case_sec"], 2.0)
        self.assertEqual(row["last_judging"]["test_name"], "002.in")

    def test_shared_batch_keeps_host_samples_independent(self) -> None:
        batch_id = self._batch(5)
        host_a = self._lease_cases(batch_id, hostname="host-a", limit=2)
        host_b = self._lease_cases(batch_id, hostname="host-b", limit=3)
        for hostname, rows, end in (("host-a", host_a, 4.0), ("host-b", host_b, 9.0)):
            case_ids = [int(row["id"]) for row in rows]
            self.scheduler.record_batch_leased(
                hostname, batch_id, case_ids, leased_monotonic=0.0
            )
            for case_id in case_ids:
                self._report(hostname, case_id, end)

        telemetry = self.scheduler.host_telemetry_snapshot()
        self.assertEqual(telemetry["host-a"]["recent_avg_per_case_sec"], 2.0)
        self.assertEqual(telemetry["host-b"]["recent_avg_per_case_sec"], 3.0)

    def test_median_uses_only_last_ten_fetch_batches(self) -> None:
        for duration in range(1, 12):
            batch_id = self._batch(1)
            row = self._lease_cases(batch_id, hostname="host-a", limit=1)[0]
            case_id = int(row["id"])
            self.scheduler.record_batch_leased(
                "host-a", batch_id, [case_id], leased_monotonic=0.0
            )
            self._report("host-a", case_id, float(duration))

        row = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(row["judged_case_count"], 11)
        self.assertEqual(row["recent_avg_per_case_sec"], 6.5)

    def test_host_release_discards_incomplete_fetch_batch(self) -> None:
        first_batch = self._batch(2)
        rows = self._lease_cases(first_batch, hostname="host-a", limit=2)
        case_ids = [int(row["id"]) for row in rows]
        self.scheduler.record_batch_leased(
            "host-a", first_batch, case_ids, leased_monotonic=0.0
        )
        self._report("host-a", case_ids[0], 1.0)
        self.scheduler.release_host_leases("host-a", now_text=_NOW)

        second_batch = self._batch(1)
        row = self._lease_cases(second_batch, hostname="host-a", limit=1)[0]
        case_id = int(row["id"])
        self.scheduler.record_batch_leased(
            "host-a", second_batch, [case_id], leased_monotonic=2.0
        )
        self._report("host-a", case_id, 5.0)

        telemetry = self.scheduler.host_telemetry_snapshot()["host-a"]
        self.assertEqual(telemetry["judged_case_count"], 2)
        self.assertEqual(telemetry["recent_avg_per_case_sec"], 3.0)


if __name__ == "__main__":
    unittest.main()
