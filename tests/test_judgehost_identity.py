from __future__ import annotations

import unittest

from app.service.judgehost.identity import (
    compile_key,
    domjudge_job_id,
    domjudge_submit_id,
)
from app.service.verification.identity import canonical_verification_id


class TestJudgehostIdentity(unittest.TestCase):
    def test_verification_id_maps_directly_to_signed_64_bit_domain(self) -> None:
        self.assertEqual(domjudge_job_id("ver-1"), 1)
        self.assertEqual(domjudge_job_id("ver-7fffffffffffffff"), (1 << 63) - 1)
        with self.assertRaisesRegex(RuntimeError, "invalid verification id"):
            domjudge_job_id("ver-8000000000000000")

    def test_verification_id_rejects_noncanonical_tokens(self) -> None:
        for token in (
            "",
            "verification",
            "ver-0",
            "ver-01",
            "ver-test",
            "ver-AB",
            "ver-1-extra",
        ):
            with self.subTest(token=token), self.assertRaisesRegex(
                RuntimeError,
                "invalid verification id",
            ):
                canonical_verification_id(token)

    def test_compile_key_contains_every_compile_input(self) -> None:
        baseline = {
            "source_hash": "1" * 64,
            "compile_hash": "2" * 32,
            "compile_config": {"toolchain_cmd_digest": "3" * 64, "script_timelimit": 30},
            "entry_point": "Main",
            "memory_limit": 262144,
        }
        expected = compile_key(**baseline)
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        self.assertEqual(compile_key(**baseline), expected)

        variants = (
            {**baseline, "source_hash": "4" * 64},
            {**baseline, "compile_hash": "5" * 32},
            {**baseline, "compile_config": {**baseline["compile_config"], "script_timelimit": 31}},
            {**baseline, "entry_point": "Other"},
            {**baseline, "memory_limit": 262145},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(compile_key(**variant), expected)

    def test_compile_key_maps_to_signed_64_bit_domain(self) -> None:
        self.assertEqual(domjudge_submit_id("0" * 64), 0)
        self.assertEqual(domjudge_submit_id(f"{(1 << 63) - 1:064x}"), (1 << 63) - 1)
        self.assertEqual(domjudge_submit_id(f"{1 << 63:064x}"), 0)
        with self.assertRaisesRegex(RuntimeError, "invalid compile key"):
            domjudge_submit_id("not-a-hash")


if __name__ == "__main__":
    unittest.main()
