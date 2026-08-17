import json
import tempfile
import unittest
from pathlib import Path

from app.service.export.adapters.nowcoder import NowcoderPackageAdapter
from app.service.problem_package.manifest import (
    VerifiedRevisionManifest,
    VerifiedTestEntry,
    describe_file,
)
from app.service.problem_package.service import VerifiedRevisionReader
from app.service.problem_package.store import MaterializationRow


class TestNowcoderExportPackage(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="nowcoder-export-package-"
        )
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_flat_layout_uses_manifest_order_and_warns_about_set_test_case(self) -> None:
        reader = self._reader(
            mode="pass-fail",
            pass_limit=1,
            checker_source="int main() { setTestCase(1); }\n",
        )
        adapter = NowcoderPackageAdapter()
        plan = adapter.plan(reader)
        target = self.root / "package"

        warning = adapter.build(
            reader,
            target=target,
            canonical_problem_slug="owner/problem",
            plan=plan,
        )

        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            ["1.ans", "1.in", "2.ans", "2.in", "checker.cc"],
        )
        self.assertEqual((target / "1.in").read_bytes(), b"first input\n")
        self.assertEqual((target / "1.ans").read_bytes(), b"first answer\n")
        self.assertEqual((target / "2.in").read_bytes(), b"second input\n")
        self.assertEqual((target / "2.ans").read_bytes(), b"second answer\n")
        self.assertEqual(
            (target / "checker.cc").read_text(encoding="utf-8"),
            "int main() { setTestCase(1); }\n",
        )
        self.assertIn("setTestCase", warning)

    def test_clean_or_absent_checker_does_not_warn(self) -> None:
        adapter = NowcoderPackageAdapter()
        clean = self._reader(
            mode="pass-fail",
            pass_limit=1,
            checker_source="int main() { return 0; }\n",
        )
        absent = self._reader(
            mode="pass-fail",
            pass_limit=1,
            checker_source=None,
            directory_name="without-checker",
        )

        self.assertEqual(adapter.plan(clean).warning, "")
        self.assertEqual(adapter.plan(absent).warning, "")

    def test_rejects_interactive_and_multi_pass_problems(self) -> None:
        adapter = NowcoderPackageAdapter()
        readers = (
            self._reader(
                mode="interactive",
                pass_limit=1,
                checker_source=None,
                directory_name="interactive",
            ),
            self._reader(
                mode="pass-fail",
                pass_limit=2,
                checker_source=None,
                directory_name="multi-pass",
            ),
        )

        for reader in readers:
            with self.subTest(
                mode=reader.manifest["mode"],
                pass_limit=reader.manifest["pass_limit"],
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "supports only single-pass pass-fail",
                ):
                    adapter.build(
                        reader,
                        target=self.root / (
                            f"unsupported-{reader.manifest['mode']}-"
                            f"{reader.manifest['pass_limit']}"
                        ),
                        canonical_problem_slug="owner/problem",
                    )

    def _reader(
        self,
        *,
        mode: str,
        pass_limit: int,
        checker_source: str | None,
        directory_name: str = "verified",
    ) -> VerifiedRevisionReader:
        package_root = self.root / directory_name
        (package_root / "config").mkdir(parents=True)
        build_config: dict[str, object] = {"generator_sources": []}
        if checker_source is not None:
            checker = package_root / "checkers" / "selected.cpp"
            checker.parent.mkdir()
            checker.write_text(checker_source, encoding="utf-8")
            build_config["checker_source"] = "checkers/selected.cpp"
        (package_root / "config" / "build.json").write_text(
            json.dumps(build_config) + "\n",
            encoding="utf-8",
        )

        tests: list[VerifiedTestEntry] = []
        for test_id, input_payload, answer_payload in (
            ("001", b"first input\n", b"first answer\n"),
            ("custom", b"second input\n", b"second answer\n"),
        ):
            test_root = package_root / "test-data" / "tests" / test_id
            test_root.mkdir(parents=True)
            input_path = test_root / "input"
            answer_path = test_root / "answer"
            input_path.write_bytes(input_payload)
            answer_path.write_bytes(answer_payload)
            tests.append(
                {
                    "id": test_id,
                    "kind": "manual",
                    "sample": False,
                    "input": describe_file(input_path, root=package_root),
                    "answer": describe_file(answer_path, root=package_root),
                }
            )

        materialization: MaterializationRow = {
            "id": f"pm-{directory_name}",
            "problem_id": 1,
            "source_commit": "a" * 40,
            "revision_number": 1,
            "source_digest": "b" * 64,
            "archive_rel_path": "materializations/verified.zip",
            "archive_sha256": "c" * 64,
            "archive_size_bytes": 1,
            "verification_id": "ver-nowcoder",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "checked_at": "2026-01-01T00:00:00Z",
            "unavailable_reason": "",
        }
        manifest: VerifiedRevisionManifest = {
            "source_commit": materialization["source_commit"],
            "revision_number": materialization["revision_number"],
            "source_digest": materialization["source_digest"],
            "mode": mode,
            "pass_limit": pass_limit,
            "verification": {
                "id": materialization["verification_id"],
                "source": "full-verification",
            },
            "solutions": [],
            "tests": tests,
        }
        return VerifiedRevisionReader(
            verified_revision=materialization,
            root=package_root,
            manifest=manifest,
        )


if __name__ == "__main__":
    unittest.main()
