import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import cast

from app.config import build_config_values
from app.service.execution.model import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_ONLY,
    ExecutionPassResult,
    ExecutionResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.runtime_config import (
    default_problem_config,
    dumps_problem_config,
    problem_config_limits,
)
from app.service.problem.test_spec import dumps_tests_spec
from app.service.statement.examples import (
    StatementExamplesBundle,
    StatementExamplesProducer,
)
from app.service.verification.detail_read_model import VerificationDetailReadModel
from app.service.verification.service import VerificationService
from app.service.verification.task_store import VerificationTaskRow
from app.service.verification.types import VerificationStatus, VerificationTaskStatus


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


class _VerificationEvidence:
    def __init__(
        self,
        root: Path,
        *,
        mode: str,
        tests_meta_rows: list[dict[str, object]],
        tasks: list[VerificationTaskRow],
    ) -> None:
        self.root = root
        self.descriptors: dict[str, PayloadFile] = {}
        self.read_model = cast(
            VerificationDetailReadModel,
            {
                "record": {
                    "status": VerificationStatus.OK,
                    "fail_reason": "",
                },
                "details": {"tests_meta_rows": tests_meta_rows},
                "tasks": tasks,
                "mode": mode,
            },
        )

    def put(self, name: str, payload: bytes) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        ref = f"blob://sha256/{digest}"
        self.descriptors[ref] = PayloadFile(
            path=path,
            size=len(payload),
            identity=digest,
            blob_ref=ref,
        )
        return ref

    def verification_detail_read_model(
        self, verification_id: str
    ) -> VerificationDetailReadModel | None:
        return self.read_model if verification_id == "ver-examples" else None

    def artifact_descriptor(self, token: str) -> PayloadFile | None:
        return self.descriptors.get(token)


def _pass(
    number: int,
    *,
    input_ref: str = "",
    output_ref: str = "",
    transcript_ref: str = "",
    capture_status: str = CAPTURE_COMPLETE,
) -> ExecutionPassResult:
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
            input_ref=input_ref,
            output_ref=output_ref,
            transcript_ref=transcript_ref,
        ),
    )


def _task(test_name: str, passes: tuple[ExecutionPassResult, ...]) -> VerificationTaskRow:
    return cast(
        VerificationTaskRow,
        {
            "task_kind": "main-correct",
            "program_id": "accepted",
            "test_name": test_name,
            "status": VerificationTaskStatus.DONE,
            "result": ExecutionResult(passes=passes),
        },
    )


class TestStatementExamplesProducer(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="statement-examples-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.artifacts = Path(self.temporary.name) / "artifacts"
        (self.workspace / "config").mkdir(parents=True)
        (self.workspace / "tests" / "manual").mkdir(parents=True)
        (self.workspace / "config" / "problem.json").write_text(
            dumps_problem_config(
                default_problem_config(limits=_PROBLEM_LIMITS),
                limits=_PROBLEM_LIMITS,
            ),
            encoding="utf-8",
        )

    def _write_spec(self, rows: list[dict[str, object]]) -> None:
        (self.workspace / "tests" / "spec.json").write_text(
            dumps_tests_spec(
                rows,
                document_max_bytes=_TESTS_SPEC_MAX_BYTES,
                sample_max_bytes=_SAMPLE_MAX_BYTES,
            ),
            encoding="utf-8",
        )

    def _producer(self, evidence: _VerificationEvidence) -> StatementExamplesProducer:
        return StatementExamplesProducer(cast(VerificationService, evidence))

    def _produce(
        self,
        evidence: _VerificationEvidence,
        *,
        verification_id: str = "ver-examples",
    ) -> StatementExamplesBundle:
        return self._producer(evidence).produce(
            self.workspace,
            verification_id=verification_id,
            tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
            statement_sample_max_bytes=_SAMPLE_MAX_BYTES,
            problem_limits=_PROBLEM_LIMITS,
        )

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
            self.artifacts,
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
            self.artifacts,
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
            self.artifacts,
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
        pass_one = _pass(
            1,
            input_ref=evidence.put("p1.in", b"first input\n"),
            output_ref=evidence.put("p1.out", b"first output\n"),
        )
        pass_two = _pass(
            2,
            input_ref=evidence.put("p2.in", b"second input\n"),
            output_ref=evidence.put("p2.out", b"second output\n"),
        )
        evidence.read_model["tasks"] = [_task("001.in", (pass_one, pass_two))]

        bundle = self._produce(evidence)

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "pair")
        self.assertEqual([row["number"] for row in sample["passes"]], [1, 2])
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(resources["examples/sample-1/pass-1.in"], "first input\n")
        self.assertEqual(resources["examples/sample-1/pass-2.ans"], "second output\n")
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
            self.artifacts,
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
            self.artifacts,
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
            _pass(
                1,
                input_ref=evidence.put("p1.in", b"captured one\n"),
                output_ref=evidence.put("p1.out", b"intermediate\n"),
            ),
            _pass(
                2,
                input_ref=evidence.put("p2.in", b"captured two\n"),
                output_ref=evidence.put("p2.out", b"final output\n"),
            ),
        )
        evidence.read_model["tasks"] = [_task("001.in", passes)]

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
            self.artifacts,
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
        transcript_ref = evidence.put("pass-1.transcript", transcript)
        evidence.read_model["tasks"] = [
            _task("001.in", (_pass(1, transcript_ref=transcript_ref),))
        ]

        bundle = self._produce(evidence)

        sample = bundle["context"]["samples"][0]
        self.assertEqual(sample["presentation"], "interaction")
        events = sample["passes"][0]["events"]
        self.assertEqual([event["source"] for event in events], ["interactor", "solution"])
        resources = {row["path"]: row["content"] for row in bundle["resources"]}
        self.assertEqual(len(resources), 2)
        self.assertIn("jury:\n> fake header", next(iter(resources.values())))

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
            self.artifacts,
            mode="pass-fail",
            tests_meta_rows=[
                {"index": 1, "test_name": "001.in", "id": "901", "sample": True},
                {"index": 2, "test_name": "002.in", "id": "902", "sample": True},
            ],
            tasks=[],
        )
        first = (
            _pass(
                1,
                input_ref=evidence.put("first-1.in", b"ignored first input\n"),
                output_ref=evidence.put("first-1.out", b"first intermediate\n"),
            ),
            _pass(
                2,
                input_ref=evidence.put("first-2.in", b"ignored second input\n"),
                output_ref=evidence.put("first-2.out", b"shown first output\n"),
            ),
        )
        second = (
            _pass(
                1,
                input_ref=evidence.put("second-1.in", b"second pass one input\n"),
                output_ref=evidence.put("second-1.out", b"second pass one output\n"),
            ),
            _pass(
                2,
                input_ref=evidence.put("second-2.in", b"second pass two input\n"),
                output_ref=evidence.put("second-2.out", b"second pass two output\n"),
            ),
        )
        evidence.read_model["tasks"] = [
            _task("001.in", first),
            _task("002.in", second),
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
            self.artifacts,
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
        evidence.read_model["tasks"] = [
            _task("001.in", (_pass(1, transcript_ref=evidence.put("t", b"")),))
        ]

        with self.assertRaisesRegex(RuntimeError, "requires sample_output"):
            self._produce(evidence)

    def test_incomplete_capture_and_malformed_transcript_fail_explicitly(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        evidence = _VerificationEvidence(
            self.artifacts,
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
        evidence.read_model["tasks"] = [
            _task(
                "001.in",
                (_pass(1, capture_status=CAPTURE_METADATA_ONLY),),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "not fully captured"):
            self._produce(evidence)

        evidence.read_model["mode"] = "interactive"
        evidence.read_model["tasks"] = [
            _task(
                "001.in",
                (_pass(1, transcript_ref=evidence.put("malformed", b"not runpipe")),),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "transcript is malformed"):
            self._produce(evidence)

    def test_missing_blob_and_total_resource_limit_fail_explicitly(self) -> None:
        self._write_spec([{"id": "901", "kind": "manual", "sample": True}])
        evidence = _VerificationEvidence(
            self.artifacts,
            mode="pass-fail",
            tests_meta_rows=[
                {"index": 1, "test_name": "001.in", "id": "901", "sample": True}
            ],
            tasks=[],
        )
        evidence.read_model["tasks"] = [
            _task(
                "001.in",
                (
                    _pass(
                        1,
                        input_ref="blob://sha256/missing",
                        output_ref=evidence.put("available.out", b"output\n"),
                    ),
                ),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "pass 1 input is unavailable"):
            self._produce(evidence)

        evidence.read_model["tasks"] = [
            _task(
                "001.in",
                (
                    _pass(
                        1,
                        input_ref=evidence.put("large.in", b"12345678"),
                        output_ref=evidence.put("large.out", b"abcdefgh"),
                    ),
                ),
            )
        ]
        with self.assertRaisesRegex(RuntimeError, "resources exceed byte limit"):
            self._producer(evidence).produce(
                self.workspace,
                verification_id="ver-examples",
                tests_spec_max_bytes=_TESTS_SPEC_MAX_BYTES,
                statement_sample_max_bytes=12,
                problem_limits=_PROBLEM_LIMITS,
            )


if __name__ == "__main__":
    unittest.main()
