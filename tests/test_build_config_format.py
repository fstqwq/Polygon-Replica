from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.service.problem.build_config import dumps_build_config


class TestBuildConfigFormat(unittest.TestCase):
    def test_build_config_dump_uses_schema_key_order(self) -> None:
        text = dumps_build_config(
            {
                "checker_source": "checkers/wcmp.cpp",
                "generator_sources": ["generators/gen.cpp"],
                "validator_source": "validators/validator.cpp",
                "accepted_solution_source": "solutions/std.cpp",
            }
        )

        self.assertEqual(
            text,
            '{\n'
            '  "accepted_solution_source": "solutions/std.cpp",\n'
            '  "validator_source": "validators/validator.cpp",\n'
            '  "checker_source": "checkers/wcmp.cpp",\n'
            '  "generator_sources": [\n'
            '    "generators/gen.cpp"\n'
            "  ]\n"
            "}\n",
        )
        self.assertEqual(
            list(json.loads(text).keys()),
            ["accepted_solution_source", "validator_source", "checker_source", "generator_sources"],
        )

    def test_build_config_dump_places_unknown_keys_after_known_keys(self) -> None:
        text = dumps_build_config({"z": 1, "checker_source": "checkers/wcmp.cpp", "a": 2})
        self.assertEqual(list(json.loads(text).keys()), ["checker_source", "a", "z"])

    def test_standard_checker_sources_are_lf_canonical(self) -> None:
        root = Path(__file__).resolve().parents[1] / "third_party" / "upstream" / "testlib" / "checkers"
        offenders = [path.name for path in sorted(root.glob("*.cpp")) if b"\r" in path.read_bytes()]
        self.assertEqual(offenders, [])
