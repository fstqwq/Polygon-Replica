from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.service.problem.build_config import (
    default_build_config,
    dumps_build_config,
    parse_build_config,
)
from app.service.problem.runtime_config import (
    ProblemConfig,
    ProblemConfigLimits,
    dumps_problem_config,
    parse_problem_config,
)
from app.service.problem.solution_metadata import (
    normalize_expected_behavior,
    parse_solution_desc,
    render_solution_desc,
)
from app.service.problem.source_tree import load_problem_source_tree
from app.service.problem.test_spec import dumps_tests_spec, loads_tests_spec


_PROBLEM_LIMITS = ProblemConfigLimits(100, 30000, 1, 2048, 1, 64)
_DOCUMENT_LIMIT = 256 * 1024
_SAMPLE_LIMIT = 32 * 1024


class TestBuildConfigFormat(unittest.TestCase):
    def test_build_config_dump_uses_schema_key_order(self) -> None:
        config = default_build_config()
        config.update(
            {
                "checker_source": "checkers/wcmp.cpp",
                "generator_sources": ["generators/gen.cpp"],
                "validator_source": "validators/validator.cpp",
                "accepted_solution_source": "solutions/std.cpp",
            }
        )
        text = dumps_build_config(config)

        self.assertEqual(
            list(json.loads(text).keys()),
            [
                "accepted_solution_source",
                "validator_source",
                "checker_source",
                "generator_sources",
                "generator_runs",
                "generator_args",
                "validator_args",
                "checker_args",
                "compile_jobs",
                "validate_jobs",
                "solve_jobs",
                "run_jobs",
                "run_timeout_sec",
            ],
        )
        self.assertEqual(parse_build_config(text), config)

    def test_build_config_rejects_noncanonical_shapes(self) -> None:
        canonical = default_build_config()
        invalid = (
            {**canonical, "unknown": 1},
            {key: value for key, value in canonical.items() if key != "run_jobs"},
            {**canonical, "generator_sources": "generators/gen.cpp"},
            {**canonical, "generator_runs": True},
            {**canonical, "checker_source": "checkers/../checker.cpp"},
            {**canonical, "checker_source": "checkers/checker.py"},
            {
                **canonical,
                "accepted_solution_source": "solutions/nested/std.cpp",
            },
            {
                **canonical,
                "generator_sources": [
                    "generators/gen.cpp",
                    "generators/gen.cpp",
                ],
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_build_config(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "duplicate key 'run_jobs'"):
            parse_build_config(
                dumps_build_config(canonical).rstrip("\n}")
                + ',\n  "run_jobs": 1\n}\n'
            )

    def test_problem_config_round_trip_is_exact(self) -> None:
        config = ProblemConfig(
            time_limit_ms=2500,
            memory_limit_mb=1,
            mode="interactive",
            pass_limit=2,
        )
        text = dumps_problem_config(config, limits=_PROBLEM_LIMITS)
        self.assertEqual(
            parse_problem_config(text, limits=_PROBLEM_LIMITS), config
        )
        invalid = (
            {**config, "memory_limit_mb": "1"},
            {**config, "mode": "Interactive"},
            {**config, "pass_limit": 0},
            {**config, "unknown": True},
            {key: value for key, value in config.items() if key != "mode"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_problem_config(
                    json.dumps(payload), limits=_PROBLEM_LIMITS
                )

    def test_tests_spec_round_trip_rejects_loose_json(self) -> None:
        text = dumps_tests_spec(
            [
                {
                    "id": "001",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "shown input\n",
                    "sample_output": "shown output\n",
                    "sample_output_validate": False,
                }
            ],
            document_max_bytes=_DOCUMENT_LIMIT,
            sample_max_bytes=_SAMPLE_LIMIT,
        )
        rows = loads_tests_spec(
            text,
            document_max_bytes=_DOCUMENT_LIMIT,
            sample_max_bytes=_SAMPLE_LIMIT,
        )
        self.assertEqual(rows[0]["sample"], True)
        self.assertEqual(rows[0]["sample_output_validate"], False)
        invalid = (
            '[{"id":"001","kind":"manual","sample":true}]',
            '{"tests":[{"id":"001","kind":"manual","sample":"true"}]}',
            '{"tests":[{"id":"001","kind":"manual","sample":true,"x":1}]}',
            '{"tests":[],"tests":[]}',
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                loads_tests_spec(
                    payload,
                    document_max_bytes=_DOCUMENT_LIMIT,
                    sample_max_bytes=_SAMPLE_LIMIT,
                )

    def test_solution_descriptor_has_one_canonical_vocabulary(self) -> None:
        text = render_solution_desc(
            "tle_or_correct", "first note\nsecond note"
        )
        self.assertEqual(
            parse_solution_desc(text),
            {
                "expected_behavior": "tle_or_correct",
                "note": "first note\nsecond note",
            },
        )
        for text in (
            "behavior: accepted\n",
            "verdict: accepted\n",
            "accepted\n",
            "expected: AC\n",
            "expected: accepted\nexpected: accepted\n",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_solution_desc(text)
        with self.assertRaises(ValueError):
            normalize_expected_behavior("AC")

    def test_problem_source_tree_requires_explicit_solution_and_generator_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="problem-source-tree-") as raw:
            root = Path(raw)
            for directory in (
                "config",
                "tests/generator",
                "solutions",
                "checkers",
                "generators",
            ):
                (root / directory).mkdir(parents=True, exist_ok=True)
            problem = ProblemConfig(
                time_limit_ms=2000,
                memory_limit_mb=1024,
                mode="pass-fail",
                pass_limit=1,
            )
            (root / "config/problem.json").write_text(
                dumps_problem_config(problem, limits=_PROBLEM_LIMITS),
                encoding="utf-8",
            )
            build = default_build_config()
            build.update(
                {
                    "accepted_solution_source": "solutions/std.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "generator_sources": ["generators/gen.cpp"],
                }
            )
            (root / "config/build.json").write_text(
                dumps_build_config(build), encoding="utf-8"
            )
            (root / "tests/spec.json").write_text(
                dumps_tests_spec(
                    [{"id": "001", "kind": "gen", "sample": False}],
                    document_max_bytes=_DOCUMENT_LIMIT,
                    sample_max_bytes=_SAMPLE_LIMIT,
                ),
                encoding="utf-8",
            )
            (root / "tests/generator/001.in").write_text(
                "gen 5\n", encoding="utf-8"
            )
            for relative in (
                "solutions/std.cpp",
                "checkers/checker.cpp",
                "generators/gen.cpp",
            ):
                (root / relative).write_text(
                    "int main(){return 0;}\n", encoding="utf-8"
                )
            descriptor = root / "solutions/std.cpp.desc"
            descriptor.write_text(
                render_solution_desc("accepted"), encoding="utf-8"
            )

            source = load_problem_source_tree(
                root,
                problem_limits=_PROBLEM_LIMITS,
                tests_spec_max_bytes=_DOCUMENT_LIMIT,
                statement_sample_max_bytes=_SAMPLE_LIMIT,
            )
            self.assertEqual(source.problem, problem)
            self.assertEqual(
                source.solution_behaviors,
                {"solutions/std.cpp": "accepted"},
            )

            descriptor.unlink()
            with self.assertRaisesRegex(
                ValueError, "required regular file is missing"
            ):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )
            descriptor.write_text(
                render_solution_desc("wrong_answer"), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError,
                "descriptor must use 'expected: accepted'",
            ):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )
            descriptor.write_text(
                render_solution_desc("accepted"), encoding="utf-8"
            )
            (root / "tests/generator/001.in").write_text(
                "other 5\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "not selected"):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )
            (root / "tests/generator/001.in").write_text(
                "gen 5\n", encoding="utf-8"
            )
            (root / "unused-link").symlink_to("solutions/std.cpp")
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )
            (root / "unused-link").unlink()
            (root / "README.md").write_text("not source\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root is not allowed"):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )
            (root / "README.md").unlink()
            (root / "tests/answers").mkdir()
            (root / "tests/answers/001.ans").write_text(
                "1\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "not authored source"):
                load_problem_source_tree(
                    root,
                    problem_limits=_PROBLEM_LIMITS,
                    tests_spec_max_bytes=_DOCUMENT_LIMIT,
                    statement_sample_max_bytes=_SAMPLE_LIMIT,
                )

    def test_standard_checker_sources_are_lf_canonical(self) -> None:
        root = Path(__file__).resolve().parents[1] / "third_party" / "upstream" / "testlib" / "checkers"
        offenders = [path.name for path in sorted(root.glob("*.cpp")) if b"\r" in path.read_bytes()]
        self.assertEqual(offenders, [])
