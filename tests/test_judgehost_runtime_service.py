import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.api import Judgehost
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.runtime_registry import VerificationRuntimeRegistry
from app.service.verification.judgehost_adapter import VerificationJudgehostAdapter
from tests.db_fixture import DBTestBase

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

    def test_prepare_materializes_external_payloads_before_admission(self) -> None:
        source_root = Path(self.settings.cache_root) / "prepared-test-files"
        source_root.mkdir(parents=True, exist_ok=True)
        submission_path = source_root / "solution.cpp"
        input_path = source_root / "001.in"
        answer_path = source_root / "001.ans"
        submission_path.write_bytes(b"int main(){return 0;}\n")
        input_path.write_bytes(b"input\n")
        answer_path.write_bytes(b"answer\n")

        payload = self.service.prepare_enqueue_payload(
            problem=self.problem,
            username=self.user,
            artifact_verification_id="",
            mode="pass-fail",
            submission_path=None,
            upload_content=None,
            upload_file=RuntimeBlobStore.describe_file(submission_path),
            upload_filename="solution.cpp",
            run_id=f"r-materialize-{uuid.uuid4().hex[:8]}",
            selected_tests=["001.in"],
            verification_id="ver-materialize-test",
            verification_program_id=_PROGRAM_ID,
            expected_behavior="accepted",
            verification_source="run.execute",
            verification_payload_override={
                "tests": [
                    {
                        "name": "001.in",
                        "answer_name": "001.ans",
                        "input_file": RuntimeBlobStore.describe_file(
                            input_path
                        ).to_payload(),
                        "answer_file": RuntimeBlobStore.describe_file(
                            answer_path
                        ).to_payload(),
                    }
                ],
                "run_config_json": '{"pass_limit":1}',
                "problem_limits": {
                    "time_limit_ms": 2000,
                    "memory_limit_mb": 1024,
                    "pass_limit": 1,
                },
                "source_files": {},
            },
        )

        source_descriptor = dict(payload["source_file"])
        source_blob_ref = str(source_descriptor["blob_ref"])
        self.assertTrue(source_blob_ref.startswith("blob://sha256/"))
        self.assertIsNotNone(self.runtime_blob_store.descriptor(source_blob_ref))

        verification_payload = dict(payload["verification_payload"])
        tests = list(verification_payload["tests"])
        prepared_test = dict(tests[0])
        for field in ("input_file", "answer_file"):
            descriptor = dict(prepared_test[field])
            blob_ref = str(descriptor["blob_ref"])
            self.assertTrue(blob_ref.startswith("blob://sha256/"))
            self.assertIsNotNone(self.runtime_blob_store.descriptor(blob_ref))

        submission_path.unlink()
        input_path.unlink()
        answer_path.unlink()
        self.assertEqual(
            self.runtime_blob_store.read(source_blob_ref),
            b"int main(){return 0;}\n",
        )

    def test_prepare_reports_missing_submission_source_before_admission(self) -> None:
        source_root = Path(self.settings.cache_root) / "missing-submission-source"
        source_root.mkdir(parents=True, exist_ok=True)
        source_path = source_root / "lost.cpp"
        source_path.write_text(
            f"// {uuid.uuid4().hex}\nint main(){{return 0;}}\n",
            encoding="utf-8",
        )
        descriptor = RuntimeBlobStore.describe_file(source_path)
        source_path.unlink()

        with self.assertRaisesRegex(
            RuntimeError,
            "submission source payload is unavailable: lost.cpp",
        ):
            self.service.prepare_enqueue_payload(
                problem=self.problem,
                username=self.user,
                artifact_verification_id="",
                mode="pass-fail",
                submission_path=None,
                upload_content=None,
                upload_file=descriptor,
                upload_filename="lost.cpp",
                run_id=f"r-missing-source-{uuid.uuid4().hex[:8]}",
                selected_tests=["001.in"],
                verification_id="ver-missing-submission-source",
                verification_program_id=_PROGRAM_ID,
                expected_behavior="accepted",
                verification_source="run.execute",
                verification_payload_override={
                    "tests": [
                        {
                            "name": "001.in",
                            "answer_name": "001.ans",
                            "input_file": self.runtime_blob_store.put_bytes(
                                b"input\n"
                            ).to_payload(),
                            "answer_file": self.runtime_blob_store.put_bytes(
                                b"answer\n"
                            ).to_payload(),
                        }
                    ],
                    "run_config_json": '{"pass_limit":1}',
                    "problem_limits": {
                        "time_limit_ms": 2000,
                        "memory_limit_mb": 1024,
                        "pass_limit": 1,
                    },
                    "source_files": {},
                },
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
        self.assertEqual(after_host["last_seen_at"], before_host["last_seen_at"])

    def test_host_status_keeps_latest_peer_ip(self) -> None:
        host = "judgehost-peer-display"
        self.service.domjudge_register_host(host)
        self.service.record_host_peer_addr(host, "203.0.113.10")
        before = next(
            item for item in self.service.status()["hosts"] if item["hostname"] == host
        )
        self.assertEqual(before["peer_addr"], "203.0.113.10")

        self.service.set_host_enabled(host, False)
        after = next(
            item for item in self.service.status()["hosts"] if item["hostname"] == host
        )
        self.assertEqual(after["peer_addr"], "203.0.113.10")

    def test_reset_clears_host_status(self) -> None:
        self.service.domjudge_register_host("judgehost-reset")
        self.assertTrue(self.service.status()["hosts"])
        self.service.reset_runtime_state()
        self.assertEqual(self.service.status()["hosts"], [])

    def test_expired_lease_reconcile_reads_online_window_from_policy(self) -> None:
        host = "judgehost-stale-policy"
        self.config_values.replace(
            {
                **self.config_values.snapshot(),
                "JUDGEHOST_ONLINE_WINDOW_SEC": 5,
            }
        )
        with patch(
            "app.service.judgehost.host.registry.now_iso",
            return_value="2000-01-01T00:00:00+00:00",
        ):
            self.service.domjudge_register_host(host)

        with patch(
            "app.service.judgehost.batch.runtime.JudgehostBatchRuntime.cases_for_host",
            return_value=[],
        ) as cases_for_host:
            released = self.service.reconcile_expired_verification_leases(
                "ver-policy-window"
            )

        self.assertEqual(released, [])
        cases_for_host.assert_called_once_with(host)

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
        precomputed = dict(payload["precomputed"])
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


if __name__ == "__main__":
    unittest.main()
