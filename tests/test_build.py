from __future__ import annotations

import uuid
import json
from pathlib import Path

from tests.common import SmokeBase
from app.impl.config import config
from app.services.tests_spec import loads_tests_spec

build_service = config.build_service
db = config.db
workspace_service = config.workspace_service


class TestBuild(SmokeBase):
    def test_build_service_and_repo_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "config").is_dir())
        self.assertTrue((ws / "tests" / "manual").is_dir())
        self.assertTrue((ws / "solutions").is_dir())
        self.assertTrue(callable(build_service.run_build))

    def test_solution_desc_metadata_selects_accepted_source(self) -> None:
        ws = Path(self._workspace_path())
        solutions = ws / "solutions"
        for stale in solutions.glob("0000_meta_ac_*"):
            stale.unlink(missing_ok=True)
        source_name = f"0000_meta_ac_{uuid.uuid4().hex[:8]}.cpp"
        src = solutions / source_name
        src.write_text("int main(){return 0;}\n", encoding="utf-8")
        (solutions / f"{source_name}.desc").write_text("expected: accepted\n", encoding="utf-8")

        selected = build_service._find_solution_by_expected_behavior(ws, "accepted")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, source_name)

    def test_solution_desc_metadata_selects_accepted_python_source(self) -> None:
        ws = Path(self._workspace_path())
        solutions = ws / "solutions"
        for stale in solutions.glob("0000_meta_ac_*"):
            stale.unlink(missing_ok=True)
        source_name = f"0000_meta_ac_{uuid.uuid4().hex[:8]}.py"
        src = solutions / source_name
        src.write_text("print('ok')\n", encoding="utf-8")
        (solutions / f"{source_name}.desc").write_text("expected: accepted\n", encoding="utf-8")

        selected = build_service._find_solution_by_expected_behavior(ws, "accepted")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, source_name)

    def test_tests_spec_runtime_maps_generator_commands(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "generators").mkdir(parents=True, exist_ok=True)
        (ws / "generators" / "gen.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        manual_dir = ws / "tests" / "manual"
        generator_dir = ws / "tests" / "generator"
        manual_dir.mkdir(parents=True, exist_ok=True)
        generator_dir.mkdir(parents=True, exist_ok=True)
        (manual_dir / "001.in").write_text("1 2\n", encoding="utf-8")
        (generator_dir / "002.in").write_text("gen 1000 3\n", encoding="utf-8")
        spec_path = ws / "tests" / "spec.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [
                        {"id": "001", "kind": "manual", "sample": True},
                        {"id": "002", "kind": "gen", "sample": False},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        entries = build_service._load_tests_spec(ws)
        self.assertIsNotNone(entries)
        runtime, targets = build_service._prepare_tests_spec_runtime(ws, entries or [], ws / ".tmp-bin")
        self.assertEqual(len(runtime), 2)
        self.assertEqual(runtime[0].get("kind"), "manual")
        self.assertEqual(runtime[0].get("id"), "001")
        self.assertTrue(bool(runtime[0].get("sample")))
        self.assertEqual(runtime[1].get("kind"), "gen")
        self.assertEqual(runtime[1].get("id"), "002")
        self.assertEqual(runtime[1].get("args"), ["1000", "3"])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0][1].name, "gen.cpp")

    def test_tests_spec_rejects_inline_payload_entries(self) -> None:
        payload = {
            "version": 2,
            "tests": [
                {"kind": "manual", "input": "1\n"},
            ],
        }
        with self.assertRaises(ValueError):
            loads_tests_spec(json.dumps(payload))

    def test_build_requires_explicit_accepted_solution_source_config(self) -> None:
        problem = f"require-main-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Require Main")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "checkers",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        # accepted.cpp exists, but build must still fail when accepted_solution_source is not explicitly configured.
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "compile_jobs": 1,
                    "validate_jobs": 1,
                    "solve_jobs": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "third_party" / "testlib" / "testlib.h").write_text("// testlib placeholder\n", encoding="utf-8")

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "failed")
        payload = json.loads(str(row["summary_json"] or "{}"))
        self.assertIn("accepted solution source is required", str(payload.get("error") or ""))

    def test_build_failure_reports_validator_returncode_and_stderr(self) -> None:
        problem = f"val-rc-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, "Validator RC")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "checkers",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "compile_jobs": 1,
                    "validate_jobs": 1,
                    "solve_jobs": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text(
            """#include <iostream>
int main() { std::cerr << "bad test format"; return 3; }
""",
            encoding="utf-8",
        )
        (ws / "checkers" / "checker.cpp").write_text(
            """#include <iostream>
int main() { return 0; }
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "failed")
        payload = json.loads(str(row["summary_json"] or "{}"))
        error_text = str(payload.get("error") or "")
        self.assertIn("validator failed on 001.in (rc=3)", error_text)
        self.assertIn("stderr: bad test format", error_text)
