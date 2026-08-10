from __future__ import annotations

from unittest.mock import patch

from app.impl.runtime.config import config
from app.impl.workspace.sample_output_validation import validate_custom_sample_outputs
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.plan import VerificationTestPlan
from tests.common import E2ETestBase
from tests.identity_helpers import canonical_test_verification_id


class TestPreview(E2ETestBase):
    seed_default_workspace = True

    def test_validate_custom_sample_outputs_uses_exact_diff_without_checker(self) -> None:
        verification_id = canonical_test_verification_id(self.random_id("ver-validate-sample"))
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        logs_root = artifact_root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        plan = VerificationTestPlan(
            test_name="001.in",
            source_kind="manual",
            display_source_path="manual_validate.cpp",
            execution_source_name="manual_validate.cpp",
            execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
            execution_input_file=config.runtime_blob_store.put_bytes(b"1\n"),
            extra_source_files={},
            tests_meta={},
            sample=True,
            sample_input_custom=False,
            sample_input_text="",
            uses_custom_sample_input=False,
            sample_output_text="ok\n",
            sample_output_validate=True,
        )
        calls: list[tuple[str, list[str], str]] = []

        def _fake_enqueue_task(**kwargs):
            calls.append(
                (
                    str(kwargs["upload_filename"]),
                    list(kwargs["selected_tests"]),
                    str(kwargs["verification_program_id"]),
                )
            )
            return "jt-sanity-ok"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            self.assertEqual(task_id, "jt-sanity-ok")
            self.assertEqual(test_name, "001.in")
            return {
                "status": "ok",
                "error": "",
                "summary": {
                    "tests": [
                        {
                            "test": "001.in",
                            "verdict": "OK",
                            "message": "",
                        }
                    ]
                },
            }

        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            config.judgehost_task_service,
            "close_programs",
        ):
            result = validate_custom_sample_outputs(
                problem="alice/sample",
                user="alice",
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_root,
                test_plans=[plan],
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_count, 1)
        self.assertEqual(
            calls,
            [
                (
                    "custom_sample_output.py",
                    ["001.in"],
                    "sanity-sample-output-0",
                )
            ],
        )
        self.assertEqual((logs_root / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n")

    def test_validate_custom_sample_outputs_uses_custom_input_without_sample_only(self) -> None:
        verification_id = canonical_test_verification_id(self.random_id("ver-validate-sample-custom-input"))
        artifact_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        logs_root = artifact_root / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        answer_file = config.runtime_blob_store.put_bytes(b"custom-answer\n")
        plan = VerificationTestPlan(
            test_name="001.in",
            source_kind="manual",
            display_source_path="manual_validate.cpp",
            execution_source_name="manual_validate.cpp",
            execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
            execution_input_file=config.runtime_blob_store.put_bytes(b"base\n"),
            extra_source_files={},
            tests_meta={},
            sample=True,
            sample_input_custom=True,
            sample_input_text="custom-input\n",
            uses_custom_sample_input=False,
            sample_output_text="custom-answer\n",
            sample_output_validate=True,
        )
        calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs):
            calls.append(dict(kwargs))
            return "jt-main-ok" if len(calls) == 1 else "jt-sanity-ok"

        def _fake_wait_for_task_case_result(task_id: str, test_name: str) -> dict[str, object]:
            self.assertEqual(test_name, "001.in")
            if task_id == "jt-main-ok":
                return {
                    "status": "ok",
                    "error": "",
                    "summary": {
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "OK",
                                "message": "",
                                "output_ref": answer_file.blob_ref,
                            }
                        ]
                    },
                }
            self.assertEqual(task_id, "jt-sanity-ok")
            return {
                "status": "ok",
                "error": "",
                "summary": {
                    "tests": [
                        {
                            "test": "001.in",
                            "verdict": "OK",
                            "message": "",
                        }
                    ]
                },
            }

        def _fake_case_output(task_id: str, test_name: str):
            self.assertEqual(task_id, "jt-main-ok")
            self.assertEqual(test_name, "001.in")
            return (answer_file.blob_ref, 1)

        payload_base = {
            "run_config_json": "{}",
            "problem_limits": {"time_limit_ms": 2000, "memory_limit_mb": 1024, "pass_limit": 1},
            "source_files": {},
        }
        with patch.object(config.judgehost_task_service, "enqueue_task", side_effect=_fake_enqueue_task), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            config.judgehost_task_service,
            "domjudge_case_output_for_task",
            side_effect=_fake_case_output,
        ), patch.object(
            config.judgehost_task_service,
            "close_programs",
        ):
            result = validate_custom_sample_outputs(
                problem="alice/sample",
                user="alice",
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_root,
                test_plans=[plan],
                accepted_source_label="solutions/std.cpp",
                accepted_source_name="std.cpp",
                accepted_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
                run_verification_payload_base=payload_base,
                bypass_case_result_cache=True,
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_count, 1)
        self.assertEqual([str(call["upload_filename"]) for call in calls], ["std.cpp", "custom_sample_output.py"])
        self.assertEqual(
            [str(call["verification_program_id"]) for call in calls],
            ["sanity-accepted", "sanity-sample-output-0"],
        )
        self.assertEqual(str(calls[0]["verification_source"]), "main-correct")
        self.assertEqual(str(calls[0]["task_kind"]), "main-correct")
        self.assertTrue(all(call["bypass_case_result_cache"] is True for call in calls))
        first_payload = dict(calls[0]["prepared_payload"])
        second_payload = dict(calls[1]["prepared_payload"])
        first_test = list(dict(first_payload["verification_payload"])["tests"])[0]
        second_test = list(dict(second_payload["verification_payload"])["tests"])[0]
        first_input = config.runtime_blob_store.read(
            PayloadFile.from_payload(first_test["input_file"])
        )
        first_answer = config.runtime_blob_store.read(
            PayloadFile.from_payload(first_test["answer_file"])
        )
        second_input = config.runtime_blob_store.read(
            PayloadFile.from_payload(second_test["input_file"])
        )
        second_answer = config.runtime_blob_store.read(
            PayloadFile.from_payload(second_test["answer_file"])
        )
        self.assertEqual(first_input, b"custom-input\n")
        self.assertEqual(first_answer, b"")
        self.assertEqual(second_input, b"custom-input\n")
        self.assertEqual(second_answer, b"custom-answer\n")
        self.assertEqual((logs_root / "validate.log").read_text(encoding="utf-8"), "001.in: ok\n")

    def test_custom_sample_outputs_share_one_accepted_program_across_tests(self) -> None:
        verification_id = canonical_test_verification_id(
            self.random_id("ver-validate-sample-shared-accepted")
        )
        logs_root = (
            config.fs_manager.prepare_verification_root(verification_id).resolve()
            / "logs"
        )
        logs_root.mkdir(parents=True, exist_ok=True)
        plans = [
            VerificationTestPlan(
                test_name=test_name,
                source_kind="manual",
                display_source_path=f"manual_{test_name}.cpp",
                execution_source_name=f"manual_{test_name}.cpp",
                execution_source_file=config.runtime_blob_store.put_bytes(
                    b"int main(){return 0;}\n"
                ),
                execution_input_file=config.runtime_blob_store.put_bytes(
                    f"base-{test_name}\n".encode("utf-8")
                ),
                extra_source_files={},
                tests_meta={},
                sample=True,
                sample_input_custom=True,
                sample_input_text=f"custom-{test_name}\n",
                uses_custom_sample_input=False,
                sample_output_text=f"answer-{test_name}\n",
                sample_output_validate=True,
            )
            for test_name in ("001.in", "002.in")
        ]
        answer_refs = {
            test_name: config.runtime_blob_store.put_bytes(
                f"answer-{test_name}\n".encode("utf-8")
            ).blob_ref
            for test_name in ("001.in", "002.in")
        }
        enqueue_calls: list[dict[str, object]] = []

        def _fake_enqueue_task(**kwargs: object) -> str:
            enqueue_calls.append(dict(kwargs))
            return f"jt-{len(enqueue_calls)}"

        def _fake_wait_for_task_case_result(
            _task_id: str,
            test_name: str,
        ) -> dict[str, object]:
            return {
                "summary": {
                    "tests": [
                        {
                            "test": test_name,
                            "verdict": "OK",
                            "message": "",
                            "output_ref": answer_refs[test_name],
                        }
                    ]
                }
            }

        def _fake_case_output(
            _task_id: str,
            test_name: str,
        ) -> tuple[str, int]:
            return answer_refs[test_name], 1

        payload_base = {
            "run_config_json": "{}",
            "problem_limits": {
                "time_limit_ms": 2000,
                "memory_limit_mb": 1024,
                "pass_limit": 1,
            },
            "source_files": {},
        }
        with patch.object(
            config.judgehost_task_service,
            "enqueue_task",
            side_effect=_fake_enqueue_task,
        ), patch.object(
            config.judgehost_task_service,
            "wait_for_task_case_result",
            side_effect=_fake_wait_for_task_case_result,
        ), patch.object(
            config.judgehost_task_service,
            "domjudge_case_output_for_task",
            side_effect=_fake_case_output,
        ), patch.object(
            config.judgehost_task_service,
            "close_programs",
        ):
            result = validate_custom_sample_outputs(
                problem="alice/sample",
                user="alice",
                verification_id=verification_id,
                mode="pass-fail",
                logs_dir=logs_root,
                test_plans=plans,
                accepted_source_label="solutions/std.cpp",
                accepted_source_name="std.cpp",
                accepted_source_file=config.runtime_blob_store.put_bytes(
                    b"int main(){return 0;}\n"
                ),
                run_verification_payload_base=payload_base,
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.validated_count, 2)
        self.assertEqual(
            [
                str(call["verification_program_id"])
                for call in enqueue_calls
            ],
            [
                "sanity-accepted",
                "sanity-sample-output-0",
                "sanity-accepted",
                "sanity-sample-output-1",
            ],
        )
        self.assertEqual(
            {
                str(call["verification_program_id"])
                for call in enqueue_calls
                if str(call["verification_source"]) == "main-correct"
            },
            {"sanity-accepted"},
        )
