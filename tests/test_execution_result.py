from __future__ import annotations

import io
import tarfile
import unittest

from app.service.judgehost.pass_bundle import InvalidPassBundle, parse_pass_bundle
from app.service.verification.execution_result import (
    CAPTURE_COMPLETE,
    CAPTURE_METADATA_INPUT_ONLY,
    ExecutionPassResult,
    ExecutionUsage,
    PassArtifacts,
    execution_result_dict,
    execution_result_from_dict,
    execution_result_from_json,
    execution_result_json,
    normalize_execution_result,
)


def _artifacts(number: int, *, interactive: bool = False) -> PassArtifacts:
    prefix = f"blob://pass-{number}-"
    return PassArtifacts(
        input_ref=prefix + "input",
        output_ref="" if interactive else prefix + "output",
        transcript_ref=prefix + "transcript" if interactive else "",
        stderr_ref=prefix + "stderr",
        system_ref=prefix + "system",
        judge_message_ref=prefix + "judge",
        team_message_ref=prefix + "team",
        metadata_ref=prefix + "metadata",
        compare_metadata_ref=prefix + "compare-metadata",
    )


def _pass(
    number: int,
    *,
    usage: ExecutionUsage,
    interactive: bool = False,
) -> ExecutionPassResult:
    return ExecutionPassResult(
        number=number,
        capture_status=CAPTURE_COMPLETE,
        runresult="correct",
        verdict="OK",
        score_text="",
        answer_correct=True,
        usage=usage,
        feedback="ok",
        artifacts=_artifacts(number, interactive=interactive),
    )


def _tar(entries: list[tuple[str, bytes]], *, symlink: str = "") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            if symlink and name == symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "target"
            else:
                info.size = len(payload)
            archive.addfile(info, None if info.issym() else io.BytesIO(payload))
    return output.getvalue()


def _complete_pass_entries(number: int) -> list[tuple[str, bytes]]:
    root = f"passes/{number}"
    return [
        (f"{root}/{name}", f"{number}:{name}".encode())
        for name in (
            "input",
            "program.out",
            "program.err",
            "system.out",
            "program.meta",
            "compare.meta",
            "judgemessage.txt",
            "teammessage.txt",
        )
    ]


class TestExecutionResult(unittest.TestCase):
    def test_round_trip_sorts_passes_numerically_and_aggregates_each_usage(self) -> None:
        passes = [
            _pass(
                number,
                usage=ExecutionUsage(
                    runtime_sec=number / 100,
                    cpu_sec=(11 - number) / 100,
                    wall_sec=number / 50,
                    memory_kb=number * 10,
                ),
                interactive=True,
            )
            for number in range(10, 0, -1)
        ]
        result = normalize_execution_result(passes=passes)
        self.assertEqual([item.number for item in result.passes], list(range(1, 11)))
        self.assertEqual(result.outcome.usage.runtime_sec, 0.1)
        self.assertEqual(result.outcome.usage.cpu_sec, 0.1)
        self.assertEqual(result.outcome.usage.wall_sec, 0.2)
        self.assertEqual(result.outcome.usage.memory_kb, 100)
        self.assertEqual(execution_result_from_json(execution_result_json(result)), result)

    def test_missing_usage_in_any_pass_makes_only_that_aggregate_null(self) -> None:
        result = normalize_execution_result(
            passes=(
                _pass(1, usage=ExecutionUsage(0.1, 0.2, 0.3, 100)),
                _pass(2, usage=ExecutionUsage(0.4, None, 0.5, 200)),
            )
        )
        self.assertEqual(result.outcome.usage.runtime_sec, 0.4)
        self.assertIsNone(result.outcome.usage.cpu_sec)
        self.assertEqual(result.outcome.usage.wall_sec, 0.5)
        self.assertEqual(result.outcome.usage.memory_kb, 200)

    def test_ordinary_and_interactive_single_and_multi_pass_shapes(self) -> None:
        ordinary = normalize_execution_result(
            passes=(_pass(1, usage=ExecutionUsage()),)
        )
        ordinary_multi = normalize_execution_result(
            passes=(
                _pass(1, usage=ExecutionUsage()),
                _pass(2, usage=ExecutionUsage()),
            )
        )
        interactive = normalize_execution_result(
            passes=(_pass(1, usage=ExecutionUsage(), interactive=True),)
        )
        interactive_multi = normalize_execution_result(
            passes=(
                _pass(1, usage=ExecutionUsage(), interactive=True),
                _pass(2, usage=ExecutionUsage(), interactive=True),
            )
        )

        self.assertEqual(len(ordinary.passes), 1)
        self.assertEqual(len(ordinary_multi.passes), 2)
        self.assertTrue(all(item.artifacts.output_ref for item in ordinary_multi.passes))
        self.assertTrue(all(not item.artifacts.transcript_ref for item in ordinary_multi.passes))
        self.assertEqual(len(interactive.passes), 1)
        self.assertEqual(len(interactive_multi.passes), 2)
        self.assertTrue(all(item.artifacts.transcript_ref for item in interactive_multi.passes))
        self.assertTrue(all(not item.artifacts.output_ref for item in interactive_multi.passes))

    def test_pass_numbers_must_be_contiguous_and_artifact_modes_are_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "contiguous"):
            normalize_execution_result(
                passes=(_pass(1, usage=ExecutionUsage()), _pass(3, usage=ExecutionUsage()))
            )
        invalid = _pass(1, usage=ExecutionUsage())
        invalid = ExecutionPassResult(
            **{
                **invalid.__dict__,
                "artifacts": PassArtifacts(
                    **{
                        **invalid.artifacts.__dict__,
                        "transcript_ref": "blob://transcript",
                    }
                ),
            }
        )
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            normalize_execution_result(passes=(invalid,))

    def test_result_decoder_rejects_string_pass_number(self) -> None:
        valid = normalize_execution_result(
            passes=(_pass(1, usage=ExecutionUsage(0.1, 0.1, 0.1, 1)),)
        )
        raw = execution_result_dict(valid)
        passes = raw["passes"]
        assert isinstance(passes, list)
        passes[0]["number"] = "01"
        with self.assertRaisesRegex(ValueError, "positive integer"):
            execution_result_from_dict(raw)


class TestPassBundle(unittest.TestCase):
    def test_complete_bundle_uses_plain_decimal_pass_numbers(self) -> None:
        raw = _tar(
            [
                (".polygon-pass-bundle", b""),
                ("final-pass-number", b"2\n"),
                *_complete_pass_entries(1),
                ("passes/2/input", b"next input"),
                ("passes/2/teammessage.txt", b"final team message"),
            ]
        )
        bundle = parse_pass_bundle(
            raw,
            max_bundle_bytes=len(raw),
            max_member_bytes=1024,
        )
        assert bundle is not None
        self.assertEqual(bundle.final_pass_number, 2)
        self.assertEqual([item.number for item in bundle.passes], [1, 2])
        self.assertEqual(bundle.passes[0].capture_status, CAPTURE_COMPLETE)
        self.assertEqual(bundle.pass_files(2)["teammessage.txt"], b"final team message")

    def test_reduced_history_is_inferred_from_exact_file_set(self) -> None:
        raw = _tar(
            [
                (".polygon-pass-bundle", b""),
                ("final-pass-number", b"2"),
                ("passes/1/input", b"input"),
                ("passes/1/program.meta", b"cpu-time: 1"),
                ("passes/1/compare.meta", b"exitcode: 42"),
                ("passes/2/input", b"next"),
                ("passes/2/teammessage.txt", b""),
            ]
        )
        bundle = parse_pass_bundle(
            raw,
            max_bundle_bytes=len(raw),
            max_member_bytes=1024,
        )
        assert bundle is not None
        self.assertEqual(
            bundle.passes[0].capture_status,
            CAPTURE_METADATA_INPUT_ONLY,
        )

    def test_plain_team_message_is_not_an_envelope(self) -> None:
        self.assertIsNone(
            parse_pass_bundle(
                b"ordinary team message",
                max_bundle_bytes=1024,
                max_member_bytes=1024,
            )
        )

    def test_rejects_noncanonical_paths_links_duplicates_and_truncation(self) -> None:
        invalid_archives = [
            _tar(
                [
                    (".polygon-pass-bundle", b""),
                    ("final-pass-number", b"1"),
                    ("passes/01/input", b"x"),
                    ("passes/1/teammessage.txt", b""),
                ]
            ),
            _tar(
                [
                    (".polygon-pass-bundle", b""),
                    ("final-pass-number", b"1"),
                    ("passes/1/input", b"x"),
                    ("passes/1/teammessage.txt", b""),
                ],
                symlink="passes/1/input",
            ),
            _tar(
                [
                    (".polygon-pass-bundle", b""),
                    ("final-pass-number", b"1"),
                    ("passes/1/input", b"x"),
                    ("passes/1/input", b"y"),
                    ("passes/1/teammessage.txt", b""),
                ]
            ),
        ]
        for raw in invalid_archives:
            with self.subTest(size=len(raw)):
                with self.assertRaises(InvalidPassBundle):
                    parse_pass_bundle(
                        raw,
                        max_bundle_bytes=len(raw),
                        max_member_bytes=1024,
                    )
        with self.assertRaises(InvalidPassBundle):
            parse_pass_bundle(
                invalid_archives[0][:800],
                max_bundle_bytes=len(invalid_archives[0]),
                max_member_bytes=1024,
            )


if __name__ == "__main__":
    unittest.main()
