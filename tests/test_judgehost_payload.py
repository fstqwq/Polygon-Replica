import ast
import base64
import hashlib
import unittest
from pathlib import Path

from app.config import build_config_values
from app.service.judgehost.callback.artifact_capture import decode_callback_blob
from app.service.judgehost.callback.diagnostic_payload import parse_diagnostic_payload
from app.service.judgehost.domjudge.limits import run_memory_limit_kb
from app.service.judgehost.callback.pass_bundle import BundledPass, PassBundle
from app.service.judgehost.callback.result_normalizer import (
    CapturedCaseArtifact,
    CapturedJudgehostCase,
    normalize_captured_case,
    pass_cache_file_name,
)
from app.service.judgehost.domjudge.result import (
    bounded_feedback_bytes,
    bounded_feedback_text,
    rewrite_untrusted_runresult,
)
from app.service.judgehost.domjudge.codec import config_payload
from app.service.judgehost.domjudge.codec import decode_base64
from app.service.judgehost.domjudge.cache import executable_hash

_DISPLAY_LIMIT_BYTES = 64 * 1024


class TestJudgehostPayload(unittest.TestCase):
    @staticmethod
    def _captured_artifacts(
        overrides: dict[str, bytes] | None = None,
    ) -> dict[str, CapturedCaseArtifact]:
        payloads = {
            "program.out": b"answer\n",
            "program.err": b"",
            "system.out": b"",
            "program.meta": (
                b"cpu-time: 0.004\nwall-time: 0.005\n" b"memory-bytes: 4096\n"
            ),
            "compare.meta": b"exitcode: 42\n",
            "judgemessage.txt": b"",
            "teammessage.txt": b"",
        }
        payloads.update(overrides or {})
        return {
            name: CapturedCaseArtifact(
                content=content,
                blob_ref=f"blob-{name}",
            )
            for name, content in payloads.items()
        }

    def test_case_normalizer_uses_compare_exit_for_checker_failure(self) -> None:
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name="001.in",
                input_ref="blob-input",
                interactive=False,
                raw_runresult="compare-error",
                runtime_fallback_sec=0.001,
                score_text="0",
                run_config={"time_limit": 1.0},
                artifacts=self._captured_artifacts(
                    {
                        "compare.meta": b"exitcode: 3\n",
                        "judgemessage.txt": b"checker crashed\n",
                    }
                ),
                pass_bundle=None,
            ),
            limit_bytes=_DISPLAY_LIMIT_BYTES,
        )
        self.assertEqual(normalized.runresult, "checker-fail")
        self.assertEqual(normalized.verdict, "FL")
        self.assertEqual(normalized.memory_kb, 4)
        self.assertEqual(normalized.result.outcome.feedback, "checker crashed")

    def test_case_normalizer_classifies_runtime_terminal_results(self) -> None:
        cases = (
            ("run-error", "run-error", "RE"),
            ("internal-error", "internal-error", "FL"),
            ("wrong-answer", "wrong-answer", "WA"),
            ("correct", "correct", "OK"),
        )
        for raw_runresult, expected_runresult, expected_verdict in cases:
            with self.subTest(runresult=raw_runresult):
                normalized = normalize_captured_case(
                    CapturedJudgehostCase(
                        test_name="001.in",
                        input_ref="blob-input",
                        interactive=False,
                        raw_runresult=raw_runresult,
                        runtime_fallback_sec=0.001,
                        score_text="",
                        run_config={"time_limit": 1.0},
                        artifacts=self._captured_artifacts(),
                        pass_bundle=None,
                        debug_text="runtime diagnostic",
                    ),
                    limit_bytes=_DISPLAY_LIMIT_BYTES,
                )

                self.assertEqual(normalized.runresult, expected_runresult)
                self.assertEqual(normalized.verdict, expected_verdict)
                self.assertIn(
                    "runtime diagnostic",
                    normalized.result.outcome.feedback,
                )

    def test_case_normalizer_detects_output_limit_without_compare_metadata(
        self,
    ) -> None:
        artifacts = self._captured_artifacts(
            {
                "program.meta": (
                    b"cpu-time: 0.004\nwall-time: 0.005\n"
                    b"memory-bytes: 4096\nstdout-bytes: 2048\n"
                    b"output-truncated: true\n"
                ),
            }
        )
        artifacts.pop("compare.meta")

        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name="001.in",
                input_ref="blob-input",
                interactive=False,
                raw_runresult="run-error",
                runtime_fallback_sec=0.001,
                score_text="0",
                run_config={"time_limit": 1.0, "output_limit": 2},
                artifacts=artifacts,
                pass_bundle=None,
            ),
            limit_bytes=_DISPLAY_LIMIT_BYTES,
        )

        self.assertEqual(normalized.runresult, "output-limit")
        self.assertEqual(normalized.verdict, "FL")
        self.assertEqual(normalized.result.passes, ())

    def test_case_normalizer_preserves_warning_when_artifacts_are_incomplete(
        self,
    ) -> None:
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name="001.in",
                input_ref="blob-input",
                interactive=False,
                raw_runresult="run-error",
                runtime_fallback_sec=0.125,
                score_text="0",
                run_config={"time_limit": 1.0},
                artifacts={
                    "program.out": CapturedCaseArtifact(
                        content=b"partial\n",
                        blob_ref="blob-program-out",
                    ),
                },
                pass_bundle=None,
                capture_warning="final artifact metadata is incomplete",
            ),
            limit_bytes=_DISPLAY_LIMIT_BYTES,
        )

        self.assertEqual(normalized.verdict, "RE")
        self.assertEqual(normalized.runtime_sec, 0.125)
        self.assertEqual(
            tuple(warning.message for warning in normalized.result.warnings),
            ("final artifact metadata is incomplete",),
        )
        self.assertEqual(normalized.result.passes, ())

    def test_case_normalizer_uses_transcript_for_interactive_output(self) -> None:
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name="001.in",
                input_ref="blob-input",
                interactive=True,
                raw_runresult="correct",
                runtime_fallback_sec=0.001,
                score_text="1",
                run_config={"time_limit": 1.0},
                artifacts=self._captured_artifacts(),
                pass_bundle=None,
            ),
            limit_bytes=_DISPLAY_LIMIT_BYTES,
        )

        final_pass = normalized.result.final_pass
        assert final_pass is not None
        self.assertEqual(final_pass.artifacts.output_ref, "")
        self.assertEqual(
            final_pass.artifacts.transcript_ref,
            "blob-program.out",
        )
        self.assertEqual(
            normalized.result.output_run_ref,
            "blob-program.out",
        )

    def test_case_normalizer_preserves_multi_pass_evidence(self) -> None:
        pass_files = {
            "input": b"1\n",
            "program.out": b"first\n",
            "program.err": b"",
            "system.out": b"",
            "program.meta": (
                b"time-used: cpu-time\ncpu-time: 0.002\n"
                b"wall-time: 0.003\nmemory-bytes: 2048\n"
            ),
            "compare.meta": b"exitcode: 42\n",
            "judgemessage.txt": b"",
            "teammessage.txt": b"",
        }
        bundle = PassBundle(
            final_pass_number=2,
            passes=(
                BundledPass(
                    number=1,
                    capture_status="complete",
                    files=pass_files,
                ),
                BundledPass(
                    number=2,
                    capture_status="complete",
                    files={**pass_files, "input": b"2\n"},
                ),
            ),
            historical_feedback_bytes=None,
        )
        artifacts = self._captured_artifacts()
        for number in (1, 2):
            for name, content in bundle.pass_files(number).items():
                cache_name = pass_cache_file_name(number, name)
                artifacts[cache_name] = CapturedCaseArtifact(
                    content=content,
                    blob_ref=f"blob-{cache_name}",
                )
        normalized = normalize_captured_case(
            CapturedJudgehostCase(
                test_name="001.in",
                input_ref="unused-final-input",
                interactive=False,
                raw_runresult="correct",
                runtime_fallback_sec=0.004,
                score_text="1",
                run_config={"time_limit": 1.0},
                artifacts=artifacts,
                pass_bundle=bundle,
            ),
            limit_bytes=_DISPLAY_LIMIT_BYTES,
        )
        self.assertEqual(len(normalized.result.passes), 2)
        self.assertEqual(
            normalized.result.passes[0].artifacts.output_ref,
            "blob-pass-1-program-out",
        )
        self.assertEqual(
            normalized.result.passes[1].artifacts.input_ref,
            "blob-pass-2-input",
        )

    def test_memory_limit_conversion_is_exact_and_strict(self) -> None:
        for memory_limit_mb in (1, 2, 4, 8, 15):
            with self.subTest(memory_limit_mb=memory_limit_mb):
                self.assertEqual(
                    run_memory_limit_kb(memory_limit_mb),
                    memory_limit_mb * 1024,
                )
        for invalid in (0, -1, True, "1", None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "invalid internal memory_limit_mb",
            ):
                run_memory_limit_kb(invalid)

    def test_executable_hash_uses_domjudge_md5_contract(self) -> None:
        files: list[tuple[str, bytes, bool]] = [
            ("z-note.txt", b"", False),
            ("run", b"#!/bin/sh\necho ok\n", True),
            ("main.cpp", b"int main(){return 0;}\n", False),
        ]
        got = executable_hash(files)
        rows = sorted(files, key=lambda item: str(item[0]))
        parts: list[str] = []
        for filename, content, is_exec in rows:
            content_md5 = hashlib.md5(bytes(content)).hexdigest()
            parts.append(f"{content_md5}{filename}{'1' if is_exec else ''}")
        expected = hashlib.md5("".join(parts).encode("utf-8")).hexdigest()
        self.assertEqual(got, expected)
        self.assertRegex(got, r"^[0-9a-f]{32}$")

    def test_untrusted_non_tl_result_uses_cpu_limit(self) -> None:
        cases = (
            ("wrong-answer", 6.5, {"time_limit": 6.0}, "timelimit"),
            ("run-error", 6.1, {"time_limit_ms": 6000}, "timelimit"),
            ("wrong-answer", 5.9, {"time_limit": 6.0}, "wrong-answer"),
            ("correct", 15.0, {"time_limit": 6.0}, "correct"),
            ("run-error", 0.6, {"time_limit": 0.5}, "timelimit"),
            ("run-error", 0.4, {"time_limit": 0.5}, "run-error"),
        )
        for runresult, cpu_sec, run_config, expected in cases:
            with self.subTest(runresult=runresult, cpu_sec=cpu_sec):
                self.assertEqual(
                    rewrite_untrusted_runresult(
                        runresult,
                        cpu_sec=cpu_sec,
                        run_cfg_obj=run_config,
                    ),
                    expected,
                )

    def test_base64_decoder_requires_base64_text(self) -> None:
        blob = b"ok\n"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(decode_base64(encoded), blob)
        self.assertEqual(decode_base64(encoded.encode("ascii")), blob)
        with self.assertRaises(RuntimeError):
            decode_base64(b"binary-artifact")
        with self.assertRaises(RuntimeError):
            decode_base64("%not-base64%")

    def test_callback_blob_keeps_raw_upload_contract(self) -> None:
        blob = b"binary-artifact"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(decode_callback_blob(blob), blob)
        self.assertEqual(decode_callback_blob(encoded), blob)

    def test_callback_blob_accepts_only_canonical_wire_value_types(self) -> None:
        blob = b"binary-artifact"
        encoded = base64.b64encode(blob).decode("ascii")

        self.assertEqual(decode_callback_blob(None), b"")
        self.assertEqual(decode_callback_blob(encoded), blob)
        self.assertEqual(decode_callback_blob(blob), blob)
        self.assertEqual(decode_callback_blob(bytearray(blob)), blob)
        self.assertEqual(decode_callback_blob(memoryview(blob)), blob)
        with self.assertRaisesRegex(RuntimeError, "not valid base64"):
            decode_callback_blob("%not-base64%")
        with self.assertRaisesRegex(RuntimeError, "base64 ASCII text"):
            decode_callback_blob("not-ascii-\u2603")
        with self.assertRaisesRegex(RuntimeError, "base64 text or raw bytes"):
            decode_callback_blob(10**100)
        with self.assertRaisesRegex(RuntimeError, "base64 text or raw bytes"):
            decode_callback_blob(True)

    def test_diagnostic_payload_selects_failure_context_without_raw_blob(
        self,
    ) -> None:
        judgehost_log = base64.b64encode(
            b"setup\nComparing failed\ncompare script output: invalid\n"
        ).decode("ascii")
        raw_archive = base64.b64encode(b"x" * 96).decode("ascii")

        parsed = parse_diagnostic_payload(
            {
                "judgehostlog": judgehost_log,
                "full_debug": raw_archive,
                "disabled": "unexpected disabled-object error",
            }
        )

        self.assertIn("Comparing failed", parsed.text)
        self.assertIn("compare script output: invalid", parsed.text)
        self.assertNotIn(raw_archive, parsed.text)
        self.assertNotIn("disabled-object", parsed.text)

    def test_diagnostic_payload_owner_is_dependency_light(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "service"
            / "judgehost"
            / "callback"
            / "diagnostic_payload.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        forbidden_prefixes = (
            "app.db",
            "app.impl",
            "app.service.judgehost.batch.state",
            "app.service.verification",
        )
        self.assertFalse(
            {
                module_name
                for module_name in imported_modules
                if module_name.startswith(forbidden_prefixes)
            }
        )

    def test_feedback_text_preserves_lines_and_redacts_internal_paths(self) -> None:
        self.assertEqual(
            bounded_feedback_text(
                "\n\nfailed on pass 2\nignored",
                limit_bytes=_DISPLAY_LIMIT_BYTES,
            ),
            "failed on pass 2\nignored",
        )
        compile_output = (
            "\n"
            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/"
            "executable/compare/123/b0e49bdbe272b5206d97ca5e888a7b00/build/"
            "validator.cpp: In function 'void EachTestCase()':\n"
            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/"
            "executable/compare/123/b0e49bdbe272b5206d97ca5e888a7b00/build/"
            "validator.cpp:4:35: error: expected ';' before 'inf'\n"
        )
        expected = (
            "validator.cpp: In function 'void EachTestCase()':\n"
            "validator.cpp:4:35: error: expected ';' before 'inf'"
        )
        self.assertEqual(
            bounded_feedback_text(
                compile_output,
                limit_bytes=_DISPLAY_LIMIT_BYTES,
            ),
            expected,
        )
        self.assertEqual(
            bounded_feedback_bytes(
                compile_output.encode("utf-8"),
                limit_bytes=_DISPLAY_LIMIT_BYTES,
            ),
            expected,
        )

    def test_config_uses_kib_for_scripts_and_bytes_for_output_storage(self) -> None:
        snapshot = build_config_values().snapshot()
        config = config_payload(snapshot)
        run_output_bytes = int(snapshot["RUN_EXEC_OUTPUT_KB"]) * 1024
        compile_output_kb = int(snapshot["TOOLCHAIN_COMPILE_OUTPUT_KB"])
        stored_log_limit_bytes = int(snapshot["JUDGEHOST_STORED_LOG_LIMIT_BYTES"])
        aux_limit_bytes = int(snapshot["AUX_DISPLAY_TEXT_LIMIT_BYTES"])
        self.assertEqual(config["timelimit_overshoot"], "1s|100%")
        self.assertEqual(config["output_storage_limit"], run_output_bytes)
        self.assertEqual(config["script_filesize_limit"], compile_output_kb)
        self.assertGreaterEqual(config["script_filesize_limit"], 1024)
        self.assertLess(stored_log_limit_bytes, config["output_storage_limit"])
        self.assertNotEqual(
            config["output_storage_limit"],
            config["script_filesize_limit"],
        )
        self.assertNotEqual(
            config["script_filesize_limit"],
            (aux_limit_bytes + 1023) // 1024,
        )


if __name__ == "__main__":
    unittest.main()
