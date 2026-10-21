import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from app.config import build_config_values
from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    ExecutionPassResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import normalize_execution_result
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.problem.runtime_config import (
    default_problem_config,
    dumps_problem_config,
    problem_config_limits,
)
from app.service.problem.test_spec import TestSpecDocumentEntry, dumps_tests_spec
from app.service.statement.sample_markup import sample_shaded_lines
from app.service.statement.examples import (
    StatementExamplesBundle,
    StatementExamplesProducer,
)
from app.service.verification.lifecycle import SanityFinish, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus, VerificationTestMetadata
from tests.identity_helpers import canonical_test_verification_id
from tests.verification_service_fixture import VerificationServiceTestBase


_CONFIG_VALUES = build_config_values()
_PROBLEM_LIMITS = problem_config_limits(_CONFIG_VALUES)
_TESTS_SPEC_MAX_BYTES = 256 * 1024
_SAMPLE_MAX_BYTES = 32 * 1024


def _frame(milliseconds: int, direction: bytes, payload: bytes) -> bytes:
    seconds, millis = divmod(milliseconds, 1000)
    header = f"[{seconds:3d}.{millis:03d}s/{len(payload)}]".encode("ascii")
    return header + direction + b": " + payload + b"\n"


def _eof(milliseconds: int, direction: bytes) -> bytes:
    seconds, millis = divmod(milliseconds, 1000)
    return f"[{seconds:3d}.{millis:03d}s/0]".encode("ascii") + direction


@dataclass
class _SampleTask:
    test_name: str
    passes: tuple[ExecutionPassResult, ...]


@dataclass
class _VerificationEvidence:
    blobs: RuntimeBlobStore
    mode: str
    tests_meta_rows: list[VerificationTestMetadata]
    tasks: list[_SampleTask]
    output_markup: dict[str, str] | None = None

    def put(self, payload: bytes) -> str:
        return self.blobs.put_bytes(payload).blob_ref

    def pass_result(
        self,
        number: int,
        *,
        input_ref: str = "",
        output_ref: str = "",
        transcript_ref: str = "",
        capture_status: str = CAPTURE_COMPLETE,
    ) -> ExecutionPassResult:
        empty_ref = self.put(b"")
        captured = capture_status == CAPTURE_COMPLETE
        return ExecutionPassResult(
            number=number,
            capture_status=capture_status,
            runresult="correct",
            verdict="OK",
            score_text="",
            answer_correct=True,
            usage=ExecutionUsage(),
            feedback="",
            artifacts=PassArtifacts(
                input_ref=(input_ref or empty_ref) if captured else "",
                output_ref=output_ref,
                transcript_ref=transcript_ref,
                stderr_ref=empty_ref if captured else "",
                system_ref=empty_ref if captured else "",
                judge_message_ref=empty_ref if captured else "",
                team_message_ref=empty_ref if captured else "",
                metadata_ref=empty_ref,
                compare_metadata_ref=empty_ref,
            ),
        )


class TestStatementExamplesProducer(VerificationServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory(prefix="statement-examples-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        (self.workspace / "config").mkdir(parents=True)
        (self.workspace / "tests" / "manual").mkdir(parents=True)
        (self.workspace / "config" / "problem.json").write_text(
            dumps_problem_config(
                default_problem_config(limits=_PROBLEM_LIMITS),
                limits=_PROBLEM_LIMITS,
            ),
            encoding="utf-8",
        )

    def _write_spec(self, rows: list[TestSpecDocumentEntry]) -> None:
        (self.workspace / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                rows,
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

    def _persist_evidence(self, evidence: _VerificationEvidence) -> str:
        verification_id = canonical_test_verification_id(self.random_id("examples"))
        self._insert_verification_row(verification_id)
        detail = {
            "mode": evidence.mode,
            "pass_limit": max(len(task.passes) for task in evidence.tasks),
            "tests_meta_rows": evidence.tests_meta_rows,
            "sanity_status": "pending",
        }
        self._activate_graph(
            verification_id,
            tasks=[{
                "id": verification_task_id(verification_id, "accepted", task.test_name),
                "task_kind": "main-correct",
                "source_path": "solutions/accepted.cpp",
                "program_id": "accepted",
                "test_name": task.test_name,
                "expected_behavior": "accepted",
            } for task in evidence.tasks],
            edges=[], detail=detail,
        )
        self.verification_task_store.commit_task_completions(tuple(
            TaskCompletion(
                task_id=verification_task_id(verification_id, "accepted", task.test_name),
                status=VerificationTaskStatus.DONE, run_id="", judgehost_task_id="",
                result=normalize_execution_result(passes=task.passes, verdict="OK", answer_correct=True),
            ) for task in evidence.tasks
        ))
        self.verification_service.finish_sanity(SanityFinish.build(
            verification_id, detail={**detail, "sanity_status": "passed"},
            sample_output_markup=tuple((evidence.output_markup or {}).items()),
        ))
        return verification_id

    def _produce(
        self,
        evidence: _VerificationEvidence,
        *,
        verification_id: str = "ver-examples",
        sample_max_bytes: int = _SAMPLE_MAX_BYTES,
    ) -> StatementExamplesBundle:
        return StatementExamplesProducer(self.verification_service).produce(
            self.workspace,
            verification_id=self._persist_evidence(evidence) if verification_id else "",
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=sample_max_bytes,
            problem_limits=_PROBLEM_LIMITS,
        )

    def test_markup_requires_exact_sample_and_whole_line_boundaries(self) -> None:
        cases = (
            (b"2\n!c1;10\n!c2;20\n", b"2\n10\n20\n", (2,)),
            (b"!c1;10!c2;\n20\n", b"10\n20\n", (1,)),
            (b"!c1;10!c2; 20\n", b"10 20\n", ()),
            (b"!c1;\\!\\\\\n!c2;end", b"!\\\nend", (1,)),
            (b"!c1;old\n", b"new\n", ()),
            (b"!c2;a\n!c1;b\n", b"a\nb\n", ()),
            (b"!c1;a\n!c2;", b"a\n", ()),
            (b"unchanged\n", b"unchanged\n", ()),
        )
        for encoded, content, expected in cases:
            with self.subTest(encoded=encoded):
                self.assertEqual(sample_shaded_lines(b"MU\xf3\x01" + encoded, content), expected)

    def test_output_markup_follows_exact_display_content(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        evidence = _VerificationEvidence(
            self.runtime_blob_store, mode="pass-fail",
            tests_meta_rows=[{"index": 1, "test_name": "001.in", "id": "901", "sample": True}], tasks=[],
        )
        original = evidence.pass_result(1, input_ref=evidence.put(b"input\n"), output_ref=evidence.put(b"one\ntwo\n"))
        marked = replace(original, artifacts=replace(
            original.artifacts, output_markup_ref=evidence.put(b"MU\xf3\x01!c1;one!c2;\ntwo\n"),
        ))
        evidence.tasks = [_SampleTask("001.in", (marked,))]
        bundle = self._produce(evidence)
        output = next(resource for resource in bundle["resources"] if resource["path"].endswith(".ans"))
        self.assertEqual(output["shaded_lines"], (1,))
        self._write_spec([{"id": "901", "kind": "manual", "sample": True, "sample_output": "changed\n", "sample_output_validate": False}])
        bundle = self._produce(evidence)
        output = next(resource for resource in bundle["resources"] if resource["path"].endswith(".ans"))
        self.assertEqual(output["content"], "changed\n")
        self.assertEqual(output.get("shaded_lines", ()), ())

    def test_custom_output_markup_uses_display_test_when_generation_has_another_owner(self) -> None:
        self._write_spec([
            {"id": "901", "kind": "manual", "sample": True},
            {"id": "902", "kind": "manual", "sample": True, "sample_output": "custom\n"},
        ])
        evidence = _VerificationEvidence(
            self.runtime_blob_store, mode="pass-fail",
            tests_meta_rows=[
                {"index": 1, "test_name": "001.in", "id": "901", "sample": True},
                {"index": 2, "test_name": "002.in", "id": "902", "sample": True},
            ], tasks=[],
        )
        result = evidence.pass_result(1, input_ref=evidence.put(b"input\n"), output_ref=evidence.put(b"original\n"))
        evidence.tasks = [_SampleTask(name, (result,)) for name in ("001.in", "002.in")]
        evidence.output_markup = {"002.in": evidence.put(b"MU\xf3\x01!c1;custom\n")}
        bundle = StatementExamplesProducer(self.verification_service).produce(
            self.workspace, verification_id=self._persist_evidence(evidence),
            execution_test_name_by_source_id={"901": "001.in", "902": "001.in"},
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES, statement_sample_max_bytes=_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )
        resource = next(row for row in bundle["resources"] if row["path"] == "examples/sample-2/display.ans")
        self.assertEqual(resource["content"], "custom\n")
        self.assertEqual(resource["shaded_lines"], (1,))

    def test_authored_structured_pair_needs_no_verification(self) -> None:
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_json": {
                        "presentation": "pair",
                        "passes": [
                            {"number": 1, "input": "first\n", "output": "one\n"},
                            {"number": 2, "input": "second\n", "output": "two\n"},
                        ],
                    },
                }
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[],
            tasks=[],
        )

        bundle = self._produce(evidence, verification_id="")

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "pair")
        self.assertEqual([row["number"] for row in sample["passes"]], [1, 2])
        self.assertEqual(
            {row["content"] for row in bundle["resources"]},
            {"first\n", "one\n", "second\n", "two\n"},
        )
        self.assertEqual(
            bundle["sample_tests"],
            [
                {
                    "inputFile": "examples/sample-1/pass-1.in",
                    "outputFile": "examples/sample-1/pass-1.ans",
                }
            ],
        )

    def test_authored_structured_interaction_needs_no_verification(self) -> None:
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_json": {
                        "presentation": "interaction",
                        "passes": [
                            {
                                "number": 1,
                                "events": [
                                    {"source": "interactor", "content": "question\n"},
                                    {"source": "solution", "content": "answer\n"},
                                ],
                            }
                        ],
                    },
                }
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="interactive",
            tests_meta_rows=[],
            tasks=[],
        )

        bundle = self._produce(evidence, verification_id="")

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "interaction")
        self.assertEqual(
            [event["source"] for event in sample["passes"][0]["events"]],
            ["interactor", "solution"],
        )

    def test_multipass_pair_uses_each_pass_and_does_not_modify_sources(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        source_before = (self.workspace / "tests" / "spec.json").read_bytes()
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[
                {
                    "index": 1,
                    "test_name": "001.in",
                    "id": "901",
                    "sample": True,
                }
            ],
            tasks=[],
        )
        pass_one = evidence.pass_result(
            1,
            input_ref=evidence.put(b"first input\n"),
            output_ref=evidence.put(b"first output\n"),
        )
        pass_two = evidence.pass_result(
            2,
            input_ref=evidence.put(b"second input\n"),
            output_ref=evidence.put(b"second output\n"),
        )
        evidence.tasks = [_SampleTask("001.in", (pass_one, pass_two))]

        bundle = self._produce(evidence)

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "pair")
        self.assertEqual([row["number"] for row in sample["passes"]], [1, 2])
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(resources["examples/sample-1/pass-1.in"], "first input\n")
        self.assertEqual(resources["examples/sample-1/pass-2.ans"], "second output\n")
        self.assertEqual(
            bundle["sample_tests"],
            [
                {
                    "inputFile": "examples/sample-1/pass-1.in",
                    "outputFile": "examples/sample-1/pass-1.ans",
                }
            ],
        )
        self.assertEqual(
            (self.workspace / "tests" / "spec.json").read_bytes(), source_before
        )

    def test_complete_explicit_pair_needs_no_verification(self) -> None:
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "display input\n",
                    "sample_output": "display output\n",
                }
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[],
            tasks=[],
        )

        bundle = self._produce(evidence, verification_id="")

        self.assertEqual(bundle["verification_id"], "")
        self.assertEqual(bundle["context"]["samples"][0]["presentation"], "pair")
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(resources["examples/sample-1/display.in"], "display input\n")
        self.assertEqual(resources["examples/sample-1/display.ans"], "display output\n")

    def test_any_explicit_override_collapses_sample_to_one_pair(self) -> None:
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "display input\n",
                }
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[
                {
                    "index": 1,
                    "test_name": "001.in",
                    "id": "901",
                    "sample": True,
                }
            ],
            tasks=[],
        )
        passes = (
            evidence.pass_result(
                1,
                input_ref=evidence.put(b"captured one\n"),
                output_ref=evidence.put(b"intermediate\n"),
            ),
            evidence.pass_result(
                2,
                input_ref=evidence.put(b"captured two\n"),
                output_ref=evidence.put(b"final output\n"),
            ),
        )
        evidence.tasks = [_SampleTask("001.in", passes)]

        bundle = self._produce(evidence)

        sample = bundle["context"]["samples"][0]
        self.assertEqual(len(sample["passes"]), 1)
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(resources["examples/sample-1/display.in"], "display input\n")
        self.assertEqual(resources["examples/sample-1/display.ans"], "final output\n")

    def test_interactive_events_preserve_order_and_omit_eof(self) -> None:
        config = default_problem_config(limits=_PROBLEM_LIMITS)
        config["mode"] = "interactive"
        (self.workspace / "config" / "problem.json").write_text(
            dumps_problem_config(config, limits=_PROBLEM_LIMITS), encoding="utf-8"
        )
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        transcript = (
            _frame(19, b">", b"jury:\n> fake header\n")
            + _frame(24, b"<", "answer ✓\n".encode())
            + _eof(30, b"]")
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="interactive",
            tests_meta_rows=[
                {
                    "index": 1,
                    "test_name": "001.in",
                    "id": "901",
                    "sample": True,
                }
            ],
            tasks=[],
        )
        transcript_ref = evidence.put(transcript)
        second_transcript_ref = evidence.put(_frame(40, b">", b"second question\n")
            + _frame(44, b"<", b"second answer\n")
            + _eof(50, b"]"),
        )
        evidence.tasks = [
            _SampleTask(
                "001.in",
                (
                    evidence.pass_result(1, transcript_ref=transcript_ref),
                    evidence.pass_result(2, transcript_ref=second_transcript_ref),
                ),
            )
        ]

        bundle = self._produce(evidence)

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "interaction")
        self.assertEqual([row["number"] for row in sample["passes"]], [1, 2])
        events = sample["passes"][0]["events"]
        self.assertEqual([event["source"] for event in events], ["interactor", "solution"])
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(len(resources), 6)
        self.assertIn("jury:\n> fake header", next(iter(resources.values())))
        self.assertEqual(
            bundle["sample_tests"],
            [
                {
                    "inputFile": "examples/sample-1/compat.in",
                    "outputFile": "examples/sample-1/compat.ans",
                }
            ],
        )
        self.assertEqual(
            resources["examples/sample-1/compat.in"],
            "jury:\n> fake header\n\n",
        )
        self.assertEqual(
            resources["examples/sample-1/compat.ans"],
            "\n\nanswer ✓\n",
        )
        self.assertNotIn("second question", resources["examples/sample-1/compat.in"])
        self.assertNotIn("second answer", resources["examples/sample-1/compat.ans"])

    def test_mixed_override_and_structured_samples_share_one_bundle(self) -> None:
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "shown first input\n",
                },
                {"id": "902", "kind": "manual", "sample": True},
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[
                {"index": 1, "test_name": "001.in", "id": "901", "sample": True},
                {"index": 2, "test_name": "002.in", "id": "902", "sample": True},
            ],
            tasks=[],
        )
        first = (
            evidence.pass_result(
                1,
                input_ref=evidence.put(b"ignored first input\n"),
                output_ref=evidence.put(b"first intermediate\n"),
            ),
            evidence.pass_result(
                2,
                input_ref=evidence.put(b"ignored second input\n"),
                output_ref=evidence.put(b"shown first output\n"),
            ),
        )
        second = (
            evidence.pass_result(
                1,
                input_ref=evidence.put(b"second pass one input\n"),
                output_ref=evidence.put(b"second pass one output\n"),
            ),
            evidence.pass_result(
                2,
                input_ref=evidence.put(b"second pass two input\n"),
                output_ref=evidence.put(b"second pass two output\n"),
            ),
        )
        evidence.tasks = [
            _SampleTask("001.in", first),
            _SampleTask("002.in", second),
        ]

        bundle = self._produce(evidence)

        samples = bundle["context"]["samples"]
        self.assertEqual([len(sample["passes"]) for sample in samples], [1, 2])
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(resources["examples/sample-1/display.in"], "shown first input\n")
        self.assertEqual(resources["examples/sample-1/display.ans"], "shown first output\n")
        self.assertEqual(
            resources["examples/sample-2/pass-2.ans"], "second pass two output\n"
        )

    def test_interactive_pair_override_requires_explicit_output(self) -> None:
        config = default_problem_config(limits=_PROBLEM_LIMITS)
        config["mode"] = "interactive"
        (self.workspace / "config" / "problem.json").write_text(
            dumps_problem_config(config, limits=_PROBLEM_LIMITS), encoding="utf-8"
        )
        self._write_spec(
            [
                {
                    "id": "901",
                    "kind": "manual",
                    "sample": True,
                    "sample_input": "display input\n",
                }
            ]
        )
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="interactive",
            tests_meta_rows=[
                {
                    "index": 1,
                    "test_name": "001.in",
                    "id": "901",
                    "sample": True,
                }
            ],
            tasks=[],
        )
        evidence.tasks = [
            _SampleTask("001.in", (evidence.pass_result(1, transcript_ref=evidence.put(b"")),))
        ]

        with self.assertRaisesRegex(RuntimeError, "requires sample_output"):
            self._produce(evidence)

    def test_incomplete_capture_and_malformed_transcript_fail_explicitly(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[
                {
                    "index": 1,
                    "test_name": "001.in",
                    "id": "901",
                    "sample": True,
                }
            ],
            tasks=[],
        )
        evidence.tasks = [
            _SampleTask(
                "001.in",
                (evidence.pass_result(1, capture_status=CAPTURE_METADATA_ONLY),),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "not fully captured"):
            self._produce(evidence)

        evidence.mode = "interactive"
        evidence.tasks = [
            _SampleTask(
                "001.in",
                (evidence.pass_result(1, transcript_ref=evidence.put(b"not runpipe")),),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "transcript is malformed"):
            self._produce(evidence)

    def test_missing_blob_and_total_resource_limit_fail_explicitly(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        evidence = _VerificationEvidence(
            self.runtime_blob_store,
            mode="pass-fail",
            tests_meta_rows=[
                {"index": 1, "test_name": "001.in", "id": "901", "sample": True}
            ],
            tasks=[],
        )
        missing_input = evidence.put(b"unavailable input\n")
        descriptor = self.runtime_blob_store.descriptor(missing_input)
        assert descriptor is not None
        descriptor.path.unlink()
        evidence.tasks = [
            _SampleTask(
                "001.in",
                (
                    evidence.pass_result(
                        1,
                        input_ref=missing_input,
                        output_ref=evidence.put(b"output\n"),
                    ),
                ),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "pass 1 input is unavailable"):
            self._produce(evidence)

        evidence.tasks = [
            _SampleTask(
                "001.in",
                (
                    evidence.pass_result(
                        1,
                        input_ref=evidence.put(b"12345678"),
                        output_ref=evidence.put(b"abcdefgh"),
                    ),
                ),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "resources exceed byte limit"):
            self._produce(evidence, sample_max_bytes=12)
