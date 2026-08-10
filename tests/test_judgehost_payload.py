from __future__ import annotations

import base64
import hashlib
import unittest

from app.runtime_value import build_runtime_values
from app.service.judgehost.limits import run_memory_limit_kb
from app.service.judgehost.runtime import (
    domjudge_feedback_text_from_bytes,
    domjudge_feedback_text_from_text,
    domjudge_rewrite_untrusted_runresult,
)
from app.service.judgehost.shared import domjudge_config_from_constants
from app.service.judgehost.toolkit import DomjudgeToolkit
from app.service.platform.hashing import domjudge_executable_hash


class TestJudgehostPayload(unittest.TestCase):
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
        got = domjudge_executable_hash(files)
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
                    domjudge_rewrite_untrusted_runresult(
                        runresult,
                        cpu_sec=cpu_sec,
                        run_cfg_obj=run_config,
                    ),
                    expected,
                )

    def test_base64_decoder_requires_base64_text(self) -> None:
        blob = b"ok\n"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(DomjudgeToolkit.b64_decode(encoded), blob)
        self.assertEqual(DomjudgeToolkit.b64_decode(encoded.encode("ascii")), blob)
        with self.assertRaises(RuntimeError):
            DomjudgeToolkit.b64_decode(b"binary-artifact")
        with self.assertRaises(RuntimeError):
            DomjudgeToolkit.b64_decode("%not-base64%")

    def test_payload_blob_bytes_keeps_raw_upload_contract(self) -> None:
        blob = b"binary-artifact"
        encoded = base64.b64encode(blob).decode("ascii")
        self.assertEqual(DomjudgeToolkit.payload_blob_bytes(blob), blob)
        self.assertEqual(DomjudgeToolkit.payload_blob_bytes(encoded), blob)

    def test_feedback_text_preserves_lines_and_redacts_internal_paths(self) -> None:
        self.assertEqual(
            domjudge_feedback_text_from_text("\n\nfailed on pass 2\nignored"),
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
        self.assertEqual(domjudge_feedback_text_from_text(compile_output), expected)
        self.assertEqual(
            domjudge_feedback_text_from_bytes(compile_output.encode("utf-8")),
            expected,
        )

    def test_config_uses_kib_for_scripts_and_bytes_for_output_storage(self) -> None:
        constants = build_runtime_values()
        config = domjudge_config_from_constants(constants)
        run_output_bytes = int(constants.RUN_EXEC_OUTPUT_KB) * 1024
        compile_output_kb = int(constants.TOOLCHAIN_COMPILE_OUTPUT_KB)
        stored_log_limit_bytes = int(constants.JUDGEHOST_STORED_LOG_LIMIT_BYTES)
        aux_limit_bytes = int(constants.AUX_DISPLAY_TEXT_LIMIT_BYTES)
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
