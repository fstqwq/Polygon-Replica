import json
import tempfile
import unittest
from pathlib import Path

from app.service.problem.build_config import (
    BuildConfig,
    dumps_build_config,
    inspect_authoring_build_config,
    parse_build_config,
)
from app.service.problem.authoring_source import inspect_authoring_source
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
        config = BuildConfig(generator_sources=[])
        config.update(
            {
                "checker_source": "checkers/wcmp.cpp",
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
            ],
        )
        self.assertEqual(parse_build_config(text), config)

    def test_build_config_rejects_noncanonical_shapes(self) -> None:
        canonical = BuildConfig(generator_sources=[])
        invalid = (
            {**canonical, "unknown": 1},
            {**canonical, "checker_source": "checkers/../checker.cpp"},
            {**canonical, "checker_source": "checkers/checker.py"},
            {**canonical, "generator_sources": "generators/gen.cpp"},
            {
                **canonical,
                "generator_sources": [
                    "generators/gen.cpp",
                    "generators/gen.cpp",
                ],
            },
            {
                **canonical,
                "accepted_solution_source": "solutions/nested/std.cpp",
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_build_config(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "duplicate key 'checker_source'"):
            parse_build_config(
                '{"checker_source":"checkers/a.cpp",'
                '"checker_source":"checkers/b.cpp"}'
            )

    def test_authoring_read_can_project_only_removed_build_fields(self) -> None:
        legacy = {
            "accepted_solution_source": "solutions/std.cpp",
            "checker_source": "checkers/checker.cpp",
            "generator_sources": ["generators/gen.cpp"],
            "checker_args": ["--obsolete"],
            "run_timeout_sec": 30,
        }
        inspected = inspect_authoring_build_config(json.dumps(legacy))

        self.assertEqual(
            inspected["config"],
            {
                "accepted_solution_source": "solutions/std.cpp",
                "checker_source": "checkers/checker.cpp",
                "generator_sources": ["generators/gen.cpp"],
            },
        )
        self.assertEqual(
            inspected["removed_keys"],
            ("checker_args", "run_timeout_sec"),
        )
        self.assertEqual(inspected["error"], "")
        with self.assertRaises(ValueError):
            parse_build_config(json.dumps(legacy))

        unknown = inspect_authoring_build_config(
            json.dumps({"future_selection": "solutions/std.cpp"})
        )
        self.assertEqual(unknown["config"], {"generator_sources": []})
        self.assertEqual(unknown["removed_keys"], ())
        self.assertIn("unsupported key 'future_selection'", unknown["error"])

    def test_authoring_inspection_repairs_legacy_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="problem-authoring-") as raw:
            root = Path(raw)
            (root / "config").mkdir(parents=True)
            (root / "tests").mkdir()
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
            (root / "config/build.json").write_text(
                json.dumps({"checker_args": [], "compile_jobs": 0}),
                encoding="utf-8",
            )
            (root / "tests/spec.json").write_text(
                '{"tests": []}\n',
                encoding="utf-8",
            )

            state = inspect_authoring_source(
                root,
                problem_limits=_PROBLEM_LIMITS,
                tests_spec_max_bytes=_DOCUMENT_LIMIT,
                statement_sample_max_bytes=_SAMPLE_LIMIT,
                allow_repair=True,
            )

            self.assertTrue(state["build_normalized"])
            self.assertEqual(state["build"], {"generator_sources": []})
            self.assertEqual(
                json.loads((root / "config/build.json").read_text(encoding="utf-8")),
                {},
            )
            self.assertIn(
                "obsolete fields were removed",
                state["issues"][0]["message"],
            )

            invalid_text = '{"checker_source":"checkers/checker.py"}'
            (root / "config/build.json").write_text(
                invalid_text,
                encoding="utf-8",
            )
            invalid = inspect_authoring_source(
                root,
                problem_limits=_PROBLEM_LIMITS,
                tests_spec_max_bytes=_DOCUMENT_LIMIT,
                statement_sample_max_bytes=_SAMPLE_LIMIT,
                allow_repair=True,
            )
            self.assertFalse(invalid["build_normalized"])
            self.assertEqual(invalid["build"], {"generator_sources": []})
            self.assertEqual(
                (root / "config/build.json").read_text(encoding="utf-8"),
                invalid_text,
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
        omitted = loads_tests_spec(
            '{"tests":[{"id":"002","kind":"manual"}]}',
            document_max_bytes=_DOCUMENT_LIMIT,
            sample_max_bytes=_SAMPLE_LIMIT,
        )
        self.assertFalse(omitted[0]["sample"])
        dumped_omitted = dumps_tests_spec(
            omitted,
            document_max_bytes=_DOCUMENT_LIMIT,
            sample_max_bytes=_SAMPLE_LIMIT,
        )
        self.assertEqual(
            loads_tests_spec(
                dumped_omitted,
                document_max_bytes=_DOCUMENT_LIMIT,
                sample_max_bytes=_SAMPLE_LIMIT,
            ),
            omitted,
        )
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

    def test_problem_source_tree_uses_explicit_main_and_generator_command(
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
            build = BuildConfig(generator_sources=["generators/gen.cpp"])
            build.update(
                {
                    "accepted_solution_source": "solutions/std.cpp",
                    "checker_source": "checkers/checker.cpp",
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
                "solutions/other.cpp",
                "checkers/checker.cpp",
                "generators/gen.cpp",
                "generators/other.cpp",
            ):
                (root / relative).write_text(
                    "int main(){return 0;}\n", encoding="utf-8"
                )
            descriptor = root / "solutions/std.cpp.desc"

            source = load_problem_source_tree(
                root,
                problem_limits=_PROBLEM_LIMITS,
                tests_spec_max_bytes=_DOCUMENT_LIMIT,
                statement_sample_max_bytes=_SAMPLE_LIMIT,
            )
            self.assertEqual(source.problem, problem)
            self.assertEqual(
                source.solution_behaviors,
                {
                    "solutions/other.cpp": "unknown",
                    "solutions/std.cpp": "accepted",
                },
            )

            descriptor.write_text(
                render_solution_desc("wrong_answer"), encoding="utf-8"
            )
            source = load_problem_source_tree(
                root,
                problem_limits=_PROBLEM_LIMITS,
                tests_spec_max_bytes=_DOCUMENT_LIMIT,
                statement_sample_max_bytes=_SAMPLE_LIMIT,
            )
            self.assertEqual(
                source.solution_behaviors,
                {
                    "solutions/other.cpp": "unknown",
                    "solutions/std.cpp": "accepted",
                },
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
