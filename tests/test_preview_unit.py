import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import build_config_values
from app.service.problem.runtime_config import (
    default_problem_config,
    dumps_problem_config,
    problem_config_limits,
)
from app.service.problem.test_spec import dumps_tests_spec
from app.service.statement.constant import (
    DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
    DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
    STATEMENT_ASSETS_DIR,
    STATEMENT_EXAMPLES_REL,
)
from app.service.statement.context import pick_statement_language, statement_languages
from app.service.statement.ftl.renderer import render_ftl_template
from app.service.statement.preview import PreviewService
from app.service.statement.examples import (
    StatementExamplesBundle,
    statement_examples_require_verification,
)
from app.service.statement.render import (
    render_statement_main,
    render_statement_offline_tree,
    seed_statement_sources,
)
from app.service.statement.signature import statement_sources_signature
from app.service.verification.signature import (
    verification_fingerprint,
    verification_manifest,
    verification_signature,
)

_TESTS_SPEC_MAX_BYTES = 256 * 1024
_STATEMENT_SAMPLE_MAX_BYTES = 32 * 1024
_CONFIG_VALUES = build_config_values()
_PROBLEM_LIMITS = problem_config_limits(_CONFIG_VALUES)


class TestPreviewUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="preview-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        seed_statement_sources(self.workspace)
        for relative in (
            "config",
            "validators",
            "checkers",
            "interactors",
            "generators",
            "solutions",
            "tests/manual",
            "tests/generator",
            "third_party/testlib",
        ):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        (self.workspace / "third_party/testlib/testlib.h").write_text(
            "// testlib fixture\n",
            encoding="utf-8",
        )
        (self.workspace / "config/problem.json").write_text(
            dumps_problem_config(
                default_problem_config(limits=_PROBLEM_LIMITS),
                limits=_PROBLEM_LIMITS,
            ),
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            '{"tests": []}\n', encoding="utf-8"
        )
        self.preview = PreviewService.__new__(PreviewService)
        self.preview.db = SimpleNamespace(config_values=_CONFIG_VALUES)

    def test_statement_languages_use_stable_preferred_order(self) -> None:
        root = self.workspace / "statement-sections"
        for language in ("japanese", "arabic", "chinese"):
            (root / language).mkdir(parents=True, exist_ok=True)

        self.assertEqual(
            statement_languages(self.workspace),
            ["english", "chinese", "arabic", "japanese"],
        )
        self.assertEqual(pick_statement_language(self.workspace), "english")

    def test_statement_template_renders_language_title_and_content(self) -> None:
        statement = self.workspace / "statement"
        sections = self.workspace / "statement-sections" / "english"
        (sections / "name.tex").write_text("Rendered Title\n", encoding="utf-8")
        (sections / "legend.tex").write_text(
            "Rendered content.\n",
            encoding="utf-8",
        )

        output = render_statement_main(
            statement,
            problem_title="Fallback Title",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered = output.read_text(encoding="utf-8")
        rendered_problem = (
            statement / "rendered" / "english" / "problem.tex"
        ).read_text(encoding="utf-8")
        self.assertIn("\\graphicspath{{rendered/english/}}", rendered)
        self.assertIn("\\import{rendered/english/}{./problem.tex}", rendered)
        self.assertIn("Rendered Title", rendered_problem)
        self.assertIn("Rendered content.", rendered_problem)
        self.assertIn("\\input{examples.tex}", rendered_problem)
        self.assertTrue(
            (statement / "rendered" / "english" / "examples.tex").is_file()
        )
        self.assertFalse((self.workspace / STATEMENT_EXAMPLES_REL).exists())

    def test_offline_statement_tree_has_a_local_entrypoint_and_style(self) -> None:
        target = self.workspace / "package-output" / "english"

        entrypoint = render_statement_offline_tree(
            self.workspace,
            "english",
            target,
            problem_title="Fallback Title",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        self.assertEqual(entrypoint, target / "statements.tex")
        rendered_main = entrypoint.read_text(encoding="utf-8")
        self.assertIn(r"\input problem.tex", rendered_main)
        self.assertNotIn("rendered/english", rendered_main)
        for name in ("problem.tex", "examples.tex", "olymp.sty"):
            self.assertTrue((target / name).is_file(), name)
        self.assertEqual(
            (target / "olymp.sty").read_bytes(),
            (self.workspace / "statement/olymp.sty").read_bytes(),
        )

    def test_preview_and_offline_renderers_write_the_same_example_bundle(self) -> None:
        bundle: StatementExamplesBundle = {
            "verification_id": "ver-shared",
            "context": {
                "samples": [
                    {
                        "number": 1,
                        "presentation": "pair",
                        "passes": [
                            {
                                "number": 1,
                                "inputFile": "examples/sample-1/pass-1.in",
                                "outputFile": "examples/sample-1/pass-1.ans",
                            }
                        ],
                    }
                ]
            },
            "resources": [
                {"path": "examples/sample-1/pass-1.in", "content": "1 2\n"},
                {"path": "examples/sample-1/pass-1.ans", "content": "3\n"},
            ],
        }
        render_statement_main(
            self.workspace / "statement",
            problem_title="Shared Bundle",
            language="english",
            examples_bundle=bundle,
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )
        preview_root = self.workspace / "statement" / "rendered" / "english"
        offline_root = self.workspace / "offline" / "english"
        render_statement_offline_tree(
            self.workspace,
            "english",
            offline_root,
            problem_title="Shared Bundle",
            examples_bundle=bundle,
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        for relative in (
            "examples.tex",
            "examples/sample-1/pass-1.in",
            "examples/sample-1/pass-1.ans",
        ):
            self.assertEqual(
                (preview_root / relative).read_bytes(),
                (offline_root / relative).read_bytes(),
            )

    def test_structured_bundle_populates_legacy_sample_tests_from_first_pass(self) -> None:
        authored = self.workspace / STATEMENT_EXAMPLES_REL
        authored.write_text(
            "<#list problem.sampleTests as test>"
            "legacy:${test.inputFile}:${test.outputFile}\\n"
            "</#list>\n",
            encoding="utf-8",
        )
        bundle: StatementExamplesBundle = {
            "verification_id": "ver-legacy-projection",
            "context": {
                "samples": [
                    {
                        "number": 1,
                        "presentation": "pair",
                        "passes": [
                            {
                                "number": 1,
                                "inputFile": "examples/sample-1/pass-1.in",
                                "outputFile": "examples/sample-1/pass-1.ans",
                            },
                            {
                                "number": 2,
                                "inputFile": "examples/sample-1/pass-2.in",
                                "outputFile": "examples/sample-1/pass-2.ans",
                            },
                        ],
                    }
                ]
            },
            "resources": [
                {"path": "examples/sample-1/pass-1.in", "content": "first in\n"},
                {"path": "examples/sample-1/pass-1.ans", "content": "first out\n"},
                {"path": "examples/sample-1/pass-2.in", "content": "second in\n"},
                {"path": "examples/sample-1/pass-2.ans", "content": "second out\n"},
            ],
            "sample_tests": [
                {
                    "inputFile": "examples/sample-1/pass-1.in",
                    "outputFile": "examples/sample-1/pass-1.ans",
                }
            ],
        }

        render_statement_main(
            self.workspace / "statement",
            problem_title="Structured Compatibility",
            language="english",
            examples_bundle=bundle,
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered_root = self.workspace / "statement/rendered/english"
        self.assertEqual(
            (rendered_root / "examples.tex").read_text(encoding="utf-8"),
            "legacy:examples/sample-1/pass-1.in:"
            "examples/sample-1/pass-1.ans\\n",
        )
        self.assertEqual(
            (rendered_root / "examples/sample-1/pass-1.in").read_text(
                encoding="utf-8"
            ),
            "first in\n",
        )

    def test_statement_examples_template_is_optional_and_editable(self) -> None:
        authored = self.workspace / STATEMENT_EXAMPLES_REL
        authored.write_text(
            "Custom examples for ${problem.name}.\n",
            encoding="utf-8",
        )
        (self.workspace / "statement-sections/english/name.tex").write_text(
            "Custom Title\n",
            encoding="utf-8",
        )

        render_statement_main(
            self.workspace / "statement",
            problem_title="Custom Title",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered_examples = (
            self.workspace / "statement/rendered/english/examples.tex"
        ).read_text(encoding="utf-8")
        self.assertEqual(rendered_examples, "Custom examples for Custom Title.\n")

    def test_legacy_examples_template_works_without_structured_context(self) -> None:
        authored = self.workspace / STATEMENT_EXAMPLES_REL
        authored.write_text(
            "<#if problem.sampleTests??>"
            "<#list problem.sampleTests as test>"
            "legacy:${test.inputFile}:${test.outputFile}\\n"
            "</#list>"
            "</#if>\n",
            encoding="utf-8",
        )
        (self.workspace / "tests/manual/001.in").write_text(
            "legacy input\n", encoding="utf-8"
        )
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "legacy output\n",
                        "sample_output_validate": False,
                    }
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

        render_statement_main(
            self.workspace / "statement",
            problem_title="Legacy Examples",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered_examples = (
            self.workspace / "statement/rendered/english/examples.tex"
        ).read_text(encoding="utf-8")
        self.assertEqual(rendered_examples, "legacy:sample.001.in:sample.001.ans\\n")

    def test_invalid_authored_examples_template_does_not_use_fallback(self) -> None:
        authored = self.workspace / STATEMENT_EXAMPLES_REL
        authored.write_bytes(b"\xff")

        with self.assertRaisesRegex(RuntimeError, "examples.*valid UTF-8"):
            render_statement_main(
                self.workspace / "statement",
                problem_title="Title",
                language="english",
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
                problem_limits=_PROBLEM_LIMITS,
            )

    def test_default_examples_template_consumes_structured_samples(self) -> None:
        self.assertIn(
            "<#if (problem.examples.samples?size > 0)>",
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
        )
        rendered = render_ftl_template(
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
            {
                "problem": {
                    "sampleTests": [],
                    "examples": {
                        "samples": [
                            {
                                "number": 1,
                                "presentation": "pair",
                                "passes": [
                                    {
                                        "number": 1,
                                        "inputFile": "pair.in",
                                        "outputFile": "pair.ans",
                                    }
                                ],
                            },
                            {
                                "number": 2,
                                "presentation": "pair",
                                "passes": [
                                    {
                                        "number": 1,
                                        "inputFile": "pass-1.in",
                                        "outputFile": "pass-1.ans",
                                    },
                                    {
                                        "number": 5,
                                        "inputFile": "pass-5.in",
                                        "outputFile": "pass-5.ans",
                                    },
                                ],
                            },
                            {
                                "number": 3,
                                "presentation": "interaction",
                                "passes": [
                                    {
                                        "number": 7,
                                        "events": [
                                            {
                                                "source": "interactor",
                                                "textFile": "events/001.txt",
                                            },
                                            {
                                                "source": "solution",
                                                "textFile": "events/002.txt",
                                            },
                                            {
                                                "source": "solution",
                                                "textFile": "events/003.txt",
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "number": 4,
                                "presentation": "interaction",
                                "passes": [
                                    {
                                        "number": 1,
                                        "events": [
                                            {
                                                "source": "interactor",
                                                "textFile": "events/pass-1.txt",
                                            }
                                        ],
                                    },
                                    {
                                        "number": 3,
                                        "events": [
                                            {
                                                "source": "solution",
                                                "textFile": "events/pass-3.txt",
                                            }
                                        ],
                                    },
                                ],
                            },
                        ]
                    },
                }
            },
        )

        self.assertIn(r"\StatementSampleFile{1}{pair.in}{pair.ans}", rendered)
        self.assertIn(
            r"\StatementSamplePassFile{2}{5}{pass-5.in}{pass-5.ans}",
            rendered,
        )
        self.assertIn(r"\begin{StatementSampleInteraction}{3}", rendered)
        self.assertIn(
            r"\StatementSampleEventFile{interactor}{events/001.txt}",
            rendered,
        )
        self.assertIn(
            r"\StatementSampleEventFile{solution}{events/002.txt}"
            "\n"
            r"\StatementSampleEventFile{solution}{events/003.txt}",
            rendered,
        )
        self.assertIn(r"\begin{StatementSampleInteraction}[3]{4}", rendered)

    def test_explicit_empty_structured_samples_do_not_fall_back_to_polygon(self) -> None:
        rendered = render_ftl_template(
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
            {
                "problem": {
                    "examples": {"samples": []},
                    "sampleTests": [
                        {"inputFile": "legacy.in", "outputFile": "legacy.ans"}
                    ],
                }
            },
        )

        self.assertEqual(rendered, "")

    def test_default_examples_template_handles_missing_optional_structures(self) -> None:
        legacy = render_ftl_template(
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
            {
                "problem": {
                    "examples": {},
                    "sampleTests": [
                        {"inputFile": "old.in", "outputFile": "old.ans"}
                    ],
                }
            },
        )
        self.assertIn(r"\exmpfile{old.in}{old.ans}", legacy)

        sparse = render_ftl_template(
            DEFAULT_STATEMENT_EXAMPLES_TEMPLATE,
            {
                "problem": {
                    "sampleTests": [],
                    "examples": {
                        "samples": [
                            {"number": 1},
                            {"number": 2, "passes": [{"number": 1}]},
                            {
                                "number": 3,
                                "presentation": "interaction",
                                "passes": [{"number": 1}],
                            },
                        ]
                    },
                }
            },
        )

        self.assertIn("Sample 1 has no passes", sparse)
        self.assertIn("Sample 2 has an unsupported presentation", sparse)
        self.assertIn("Interaction sample 3, pass 1 has no events field", sparse)

    def test_statement_render_requires_main_template_and_style(self) -> None:
        statement = self.workspace / "statement"
        (statement / "olymp.sty").unlink()
        with self.assertRaisesRegex(RuntimeError, "olymp.*is missing"):
            render_statement_main(
                statement,
                problem_title="Title",
                language="english",
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
                problem_limits=_PROBLEM_LIMITS,
            )

        seed_statement_sources(self.workspace)
        (statement / "statements.ftl").unlink()
        with self.assertRaisesRegex(RuntimeError, "statements\\.ftl.*is missing"):
            render_statement_main(
                statement,
                problem_title="Title",
                language="english",
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
                problem_limits=_PROBLEM_LIMITS,
            )

    def test_statement_render_copies_shared_assets_only(self) -> None:
        sections = self.workspace / "statement-sections" / "english"
        (sections / "legacy-only.txt").write_text("legacy\n", encoding="utf-8")
        assets = self.workspace / STATEMENT_ASSETS_DIR / "figures"
        assets.mkdir(parents=True)
        (assets / "diagram.png").write_bytes(b"PNG")

        render_statement_main(
            self.workspace / "statement",
            problem_title="Title",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered = self.workspace / "statement" / "rendered" / "english"
        self.assertEqual((rendered / "figures/diagram.png").read_bytes(), b"PNG")
        self.assertFalse((rendered / "legacy-only.txt").exists())

    def test_statement_render_materializes_manual_and_generator_samples(self) -> None:
        (self.workspace / "statement/problem.tex").write_text(
            DEFAULT_STATEMENT_PROBLEM_TEMPLATE,
            encoding="utf-8",
        )
        (self.workspace / "tests/manual/001.in").write_text(
            "manual input\n",
            encoding="utf-8",
        )
        (self.workspace / "tests/generator/902.in").write_text(
            "generated input\n",
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "manual output\n",
                    },
                    {
                        "id": "902",
                        "kind": "gen",
                        "sample": True,
                        "sample_output": "generated output\n",
                    },
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

        render_statement_main(
            self.workspace / "statement",
            problem_title="Title",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered = self.workspace / "statement/rendered/english"
        self.assertEqual(
            (rendered / "sample.001.in").read_text(encoding="utf-8"),
            "manual input\n",
        )
        self.assertEqual(
            (rendered / "sample.001.ans").read_text(encoding="utf-8"),
            "manual output\n",
        )
        self.assertEqual(
            (rendered / "sample.902.in").read_text(encoding="utf-8"),
            "generated input\n",
        )
        rendered_examples = (rendered / "examples.tex").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            r"\exmpfile{sample.001.in}{sample.001.ans}%",
            rendered_examples,
        )

    def test_statement_samples_prefer_explicit_display_payloads(self) -> None:
        (self.workspace / "tests/manual/001.in").write_text(
            "materialized input\n",
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "display input\n",
                        "sample_output": "display output\n",
                    }
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

        render_statement_main(
            self.workspace / "statement",
            problem_title="Title",
            language="english",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        rendered = self.workspace / "statement/rendered/english"
        self.assertEqual(
            (rendered / "sample.001.in").read_text(encoding="utf-8"),
            "display input\n",
        )
        self.assertEqual(
            (rendered / "sample.001.ans").read_text(encoding="utf-8"),
            "display output\n",
        )

    def test_statement_sample_and_spec_budgets_are_independent(self) -> None:
        sample_limit = 1024
        document_limit = 4096
        dumps_tests_spec(
            [
                {
                    "id": "001",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "a" * 400,
                    "sample_output": "b" * 400,
                },
                {
                    "id": "002",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "c" * 400,
                    "sample_output": "d" * 400,
                },
            ],
            document_max_bytes=document_limit,
            sample_max_bytes=sample_limit,
        )
        with self.assertRaisesRegex(ValueError, "statement sample byte limit"):
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "a" * 600,
                        "sample_output": "b" * 500,
                    }
                ],
                document_max_bytes=document_limit,
                sample_max_bytes=sample_limit,
            )
        with self.assertRaisesRegex(ValueError, "tests/spec.json is too long"):
            dumps_tests_spec(
                [
                    {
                        "id": f"{index:03d}",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "x" * 300,
                    }
                    for index in range(1, 5)
                ],
                document_max_bytes=sample_limit,
                sample_max_bytes=sample_limit,
            )

    def test_statement_examples_require_verification_for_missing_or_validated_payloads(self) -> None:
        (self.workspace / "tests/manual/001.in").write_text(
            "base input\n",
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "validate me\n",
                    },
                    {
                        "id": "002",
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "display input\n",
                    },
                    {
                        "id": "003",
                        "kind": "manual",
                        "sample": True,
                        "sample_output": "display only\n",
                        "sample_output_validate": False,
                    },
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )
        (self.workspace / "tests/manual/003.in").write_text(
            "base input\n",
            encoding="utf-8",
        )

        required = statement_examples_require_verification(
            self.workspace,
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

        self.assertTrue(required)

    def test_complete_interactive_pair_does_not_require_verification(self) -> None:
        config_path = self.workspace / "config/problem.json"
        interactive_config = default_problem_config(limits=_PROBLEM_LIMITS)
        interactive_config["mode"] = "interactive"
        config_path.write_text(
            dumps_problem_config(interactive_config, limits=_PROBLEM_LIMITS),
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "001",
                        "kind": "gen",
                        "sample": True,
                        "sample_input": "question\n",
                        "sample_output": "answer\n",
                        "sample_output_validate": False,
                    }
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            statement_examples_require_verification(
                self.workspace,
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
                problem_limits=_PROBLEM_LIMITS,
            ),
            False,
        )

    def test_statement_examples_reject_invalid_test_kind(self) -> None:
        pass_fail_config = default_problem_config(limits=_PROBLEM_LIMITS)
        (self.workspace / "config/problem.json").write_text(
            dumps_problem_config(pass_fail_config, limits=_PROBLEM_LIMITS),
            encoding="utf-8",
        )
        (self.workspace / "tests/spec.json").write_text(
            '{"version":2,"tests":[{"id":"001","kind":"generator","sample":true}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid tests/spec.json"):
            statement_examples_require_verification(
                self.workspace,
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
                problem_limits=_PROBLEM_LIMITS,
            )

    def test_verification_signature_tracks_content_but_not_mtime(self) -> None:
        build = self.workspace / "config/build.json"
        build.write_text("{}\n", encoding="utf-8")
        signature = verification_signature(self.workspace)
        fingerprint = verification_fingerprint(self.workspace)
        stat_result = build.stat()
        os.utime(
            build,
            ns=(stat_result.st_atime_ns + 5_000_000_000, stat_result.st_mtime_ns + 5_000_000_000),
        )
        self.assertEqual(signature, verification_signature(self.workspace))
        self.assertNotEqual(fingerprint, verification_fingerprint(self.workspace))

        (self.workspace / "validators/validator.cpp").write_text(
            "int main() { return 0; }\n",
            encoding="utf-8",
        )
        self.assertNotEqual(signature, verification_signature(self.workspace))

    def test_verification_identity_ignores_empty_source_directories(self) -> None:
        empty = self.workspace / "interactors"
        empty.rmdir()
        signature = verification_signature(self.workspace)
        manifest_signature = verification_manifest(self.workspace).signature
        fingerprint = verification_fingerprint(self.workspace)
        empty.mkdir()
        self.assertEqual(signature, verification_signature(self.workspace))
        self.assertEqual(manifest_signature, verification_manifest(self.workspace).signature)
        self.assertEqual(fingerprint, verification_fingerprint(self.workspace))

    def test_statement_signature_tracks_generator_sample_payload(self) -> None:
        payload = self.workspace / "tests/generator/901.in"
        payload.write_text("first\n", encoding="utf-8")
        (self.workspace / "tests/spec.json").write_text(
            dumps_tests_spec(
                [
                    {
                        "id": "901",
                        "kind": "gen",
                        "sample": True,
                        "sample_output": "answer\n",
                    }
                ],
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )
        before = statement_sources_signature(
            self.workspace,
            problem_title="T",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
        )
        payload.write_text("second\n", encoding="utf-8")
        self.assertNotEqual(
            before,
            statement_sources_signature(
                self.workspace,
                problem_title="T",
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=_STATEMENT_SAMPLE_MAX_BYTES,
            ),
        )

    def test_ftl_renderer_strips_standalone_directive_lines(self) -> None:
        rendered = render_ftl_template(
            "A\n<#list problem.sampleTests as test>\nX${test.inputFile}\n</#list>\nB\n",
            {"problem": {"sampleTests": [{"inputFile": "1"}, {"inputFile": "2"}]}},
        )
        self.assertEqual(rendered, "A\nX1\nX2\nB\n")

    def test_latex_failure_detail_names_missing_runtime_component(self) -> None:
        self.assertIn(
            "missing LaTeX format file",
            self.preview._latex_compile_error_detail(
                "I can't find the format file `pdflatex.fmt'!\n",
                1,
            ),
        )
        self.assertEqual(
            self.preview._latex_compile_error_detail(
                "! LaTeX Error: File `siunitx.sty' not found.\n",
                1,
            ),
            "missing LaTeX package siunitx.sty",
        )
