from __future__ import annotations

# ascii-lint: allow; reason=chinese-test

import hashlib
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

import yaml

from app.service.export.icpc_package import (
    SUBMISSION_RULES,
    annotated_submission,
    problem_uuid,
    render_problem_yaml,
    render_submissions_yaml,
    write_input_validator,
    write_output_validator,
)
from app.service.contest.metadata import materialize_contest_problem_package
from app.service.importing.archive import ArchivePolicy, ArchiveView


class TestICPCExportPackage(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="icpc-export-package-")
        self.root = Path(self._temp_dir.name)
        self.snapshot = self.root / "snapshot"
        self.package = self.root / "package"
        (self.snapshot / "third_party" / "testlib").mkdir(parents=True)
        (self.snapshot / "third_party" / "testlib" / "testlib.h").write_text(
            "// test header\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_problem_yaml_uses_safe_unicode_and_deterministic_identity(self) -> None:
        slug = "owner/problem #1 + 中文"
        text = render_problem_yaml(
            problem_slug=slug,
            source_commit="a" * 40,
            names={"en": "O'Brien: #1", "zh": "中文题目"},
            mode="pass-fail",
            pass_limit=1,
            time_limit_ms=2250,
            memory_limit_mb=1,
        )
        metadata = yaml.safe_load(text)
        self.assertEqual(metadata["problem_format_version"], "2025-09")
        self.assertEqual(metadata["type"], "pass-fail")
        self.assertEqual(
            metadata["name"],
            {"en": "O'Brien: #1", "zh": "中文题目"},
        )
        self.assertEqual(
            metadata["uuid"],
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"polygon-replica/problem/{slug}")),
        )
        self.assertEqual(problem_uuid(slug), metadata["uuid"])
        self.assertEqual(metadata["version"], "a" * 40)
        self.assertEqual(metadata["limits"], {"time_limit": 2.25, "memory": 1})

    def test_contest_short_names_are_variants_of_one_canonical_archive(self) -> None:
        canonical = self.root / "canonical.zip"
        with zipfile.ZipFile(
            canonical,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("problem.yaml", "name: {en: Sample}\n")
            archive.writestr(
                "domjudge-problem.ini",
                "name = Sample\n"
                "externalid = sample\n"
                "short-name = sample\n"
                "timelimit = 2\n",
            )
            archive.writestr("data/secret/001.in", b"1\n")
        canonical_digest = hashlib.sha256(canonical.read_bytes()).hexdigest()

        variants: dict[str, Path] = {}
        for label in ("A", "E"):
            target = self.root / f"{label}-sample.zip"
            materialize_contest_problem_package(
                canonical,
                target,
                short_name=label,
                staging_parent=self.root / "staging",
            )
            variants[label] = target

        self.assertEqual(
            hashlib.sha256(canonical.read_bytes()).hexdigest(),
            canonical_digest,
        )
        for label, target in variants.items():
            with zipfile.ZipFile(target, "r") as archive:
                self.assertIn(
                    f"short-name = {label}\n",
                    archive.read("domjudge-problem.ini").decode("utf-8"),
                )
                self.assertEqual(
                    archive.read("problem.yaml"),
                    b"name: {en: Sample}\n",
                )
                self.assertEqual(archive.read("data/secret/001.in"), b"1\n")

    def test_contest_short_name_rewrite_rejects_ambiguous_metadata(self) -> None:
        cases = {
            "missing": {"problem.yaml": "name: Sample\n"},
            "duplicate-entry": {
                "domjudge-problem.ini": (
                    "short-name = sample\nshort-name = duplicate\n"
                )
            },
            "multiline-entry": {
                "domjudge-problem.ini": "short-name = sample\n continuation\n"
            },
        }
        for name, members in cases.items():
            with self.subTest(name=name):
                canonical = self.root / f"{name}.zip"
                with zipfile.ZipFile(canonical, "w") as archive:
                    for member, payload in members.items():
                        archive.writestr(member, payload)
                with self.assertRaises(ValueError):
                    materialize_contest_problem_package(
                        canonical,
                        self.root / f"{name}-out.zip",
                        short_name="A",
                        staging_parent=self.root / "staging",
                    )

        canonical = self.root / "valid.zip"
        with zipfile.ZipFile(canonical, "w") as archive:
            archive.writestr("domjudge-problem.ini", "short-name = sample\n")
        with self.assertRaisesRegex(
            ValueError,
            "invalid contest problem short-name",
        ):
            materialize_contest_problem_package(
                canonical,
                self.root / "unsafe-out.zip",
                short_name="A\nB",
                staging_parent=self.root / "staging",
            )

    def test_contest_transform_ignores_upload_budgets_but_rejects_symlinks(
        self,
    ) -> None:
        canonical = self.root / "canonical-over-upload-budget.zip"
        with zipfile.ZipFile(canonical, "w") as archive:
            archive.writestr("domjudge-problem.ini", "short-name = sample\n")
            archive.writestr("problem.yaml", "name: {en: Sample}\n")
            archive.writestr("data/secret/001.in", b"payload larger than one byte\n")

        with self.assertRaisesRegex(ValueError, "more than 1 entries"):
            ArchiveView(
                canonical,
                ArchivePolicy(max_entries=1, max_expanded_bytes=1024 * 1024),
            )
        with ArchiveView(
            canonical,
            ArchivePolicy(max_entries=10, max_expanded_bytes=1),
        ) as upload_view:
            with self.assertRaisesRegex(ValueError, "expanded zip payload"):
                upload_view.zip_file.open(
                    upload_view.entries["data/secret/001.in"]
                )

        transformed = self.root / "trusted-internal.zip"
        materialize_contest_problem_package(
            canonical,
            transformed,
            short_name="A",
            staging_parent=self.root / "staging",
        )
        with zipfile.ZipFile(transformed, "r") as archive:
            self.assertEqual(
                archive.read("data/secret/001.in"),
                b"payload larger than one byte\n",
            )

        unsafe = self.root / "canonical-symlink.zip"
        link = zipfile.ZipInfo("data/secret/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("domjudge-problem.ini", "short-name = sample\n")
            archive.writestr(link, "001.in")
        with self.assertRaisesRegex(ValueError, "contains a symlink"):
            materialize_contest_problem_package(
                unsafe,
                self.root / "unsafe-symlink-out.zip",
                short_name="A",
                staging_parent=self.root / "staging",
            )

    def test_problem_yaml_types_and_combined_legacy_fallback(self) -> None:
        cases = (
            ("pass-fail", 1, "2025-09", "pass-fail"),
            ("interactive", 1, "2025-09", "interactive"),
            ("pass-fail", 3, "2025-09", "multi-pass"),
            ("interactive", 3, "legacy", "interactive multi-pass"),
        )
        for mode, pass_limit, expected_version, expected_type in cases:
            with self.subTest(mode=mode, pass_limit=pass_limit):
                metadata = yaml.safe_load(
                    render_problem_yaml(
                        problem_slug="owner/problem",
                        source_commit="b" * 40,
                        names={"en": "Problem"},
                        mode=mode,
                        pass_limit=pass_limit,
                        time_limit_ms=1000,
                        memory_limit_mb=None,
                    )
                )
                self.assertEqual(metadata["problem_format_version"], expected_version)
                self.assertEqual(metadata["type"], expected_type)
                if pass_limit > 1:
                    self.assertEqual(metadata["limits"]["validation_passes"], pass_limit)
                else:
                    self.assertNotIn("validation_passes", metadata["limits"])

    def test_submissions_yaml_preserves_all_expected_result_sets(self) -> None:
        entries = {
            f"{rule['directory']}/{expected}.cpp": {
                "language": "cpp",
                "permitted": list(rule["permitted"]),
                "required": list(rule["required"]),
            }
            for expected, rule in SUBMISSION_RULES.items()
        }
        self.assertEqual(yaml.safe_load(render_submissions_yaml(entries)), entries)

    def test_mixed_annotation_is_language_legal_and_does_not_mutate_source(self) -> None:
        source = self.snapshot / "mixed.py"
        original = "print('ok')\n"
        source.write_text(original, encoding="utf-8")
        payload = annotated_submission(source, ("CORRECT", "TIMELIMIT"))
        self.assertTrue(payload.startswith(b"# @EXPECTED_RESULTS@: CORRECT,TIMELIMIT\n"))
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_validator_wrappers_map_exit_codes_and_preserve_output(self) -> None:
        validator = self.snapshot / "validator.py"
        validator.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "data = sys.stdin.read().strip()\n"
            "if len(sys.argv) >= 4:\n"
            "    Path(sys.argv[3], 'nextpass.in').write_text('next\\n')\n"
            "print('validator-output')\n"
            "raise SystemExit(int(data or '0'))\n",
            encoding="utf-8",
        )
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=validator)
        write_output_validator(snapshot=self.snapshot, package_root=self.package, source=validator)

        input_run = self.package / "input_validators" / "validator" / "run"
        accepted = subprocess.run(
            [str(input_run)], input="0\n", text=True, capture_output=True, check=False
        )
        self.assertEqual(accepted.returncode, 42)
        rejected = subprocess.run(
            [str(input_run)], input="7\n", text=True, capture_output=True, check=False
        )
        self.assertEqual(rejected.returncode, 7)

        feedback = self.root / "feedback"
        feedback.mkdir()
        output_run = self.package / "output_validator" / "run"
        wrong = subprocess.run(
            [str(output_run), "input", "answer", str(feedback)],
            input="1\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(wrong.returncode, 43)
        self.assertEqual(wrong.stdout, "validator-output\n")
        self.assertEqual((feedback / "nextpass.in").read_text(encoding="utf-8"), "next\n")
        judge_error = subprocess.run(
            [str(output_run), "input", "answer", str(feedback)],
            input="7\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(judge_error.returncode, 7)

    def test_cpp_validator_build_script_builds_and_maps_success(self) -> None:
        self.assertIsNotNone(shutil.which("c++"), "Linux test host must provide c++")
        validator = self.snapshot / "validator name.cpp"
        validator.write_text("int main() { return 0; }\n", encoding="utf-8")
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=validator)
        validator_dir = self.package / "input_validators" / "validator"
        subprocess.run([str(validator_dir / "build")], cwd=validator_dir, check=True)
        result = subprocess.run([str(validator_dir / "run")], cwd=validator_dir, check=False)
        self.assertEqual(result.returncode, 42)

    def test_missing_input_validator_gets_explicit_accept_all_program(self) -> None:
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=None)
        run = self.package / "input_validators" / "accept_all" / "run"
        result = subprocess.run(
            [str(run)], input=b"arbitrary input\n", capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 42)


if __name__ == "__main__":
    unittest.main()
