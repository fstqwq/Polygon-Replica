from __future__ import annotations

import uuid
import json
import shutil
from pathlib import Path

from tests.common import SmokeBase
from app.impl.config import config
from app.services.tests_spec import loads_tests_spec
from app.services.util import run_cmd

build_service = config.build_service
db = config.db
workspace_service = config.workspace_service


class TestBuild(SmokeBase):
    def _owned_problem(self, prefix: str) -> str:
        safe_prefix = str(prefix or "p").strip().lower().replace("_", "-")
        safe_prefix = "-".join(part for part in safe_prefix.split("-") if part) or "p"
        return f"{self.user}/{safe_prefix}-{uuid.uuid4().hex[:8]}"

    def test_build_service_and_repo_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "config").is_dir())
        self.assertTrue((ws / "tests" / "manual").is_dir())
        self.assertTrue((ws / "solutions").is_dir())
        self.assertTrue(callable(build_service.run_build))

    def test_standard_checker_uses_upstream_exit_codes_even_with_workspace_testlib_override(self) -> None:
        problem = self._owned_problem("std-checker-rc")
        workspace_service.ensure_problem(problem, "Std Checker RC")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")

        upstream_testlib = Path("third_party/upstream/testlib/testlib.h").read_text(encoding="utf-8", errors="replace")
        patched_testlib = upstream_testlib.replace("#   define OK_EXIT_CODE 42", "#   define OK_EXIT_CODE 0")
        patched_testlib = patched_testlib.replace("#   define WA_EXIT_CODE 43", "#   define WA_EXIT_CODE 1")
        patched_testlib = patched_testlib.replace("#   define PE_EXIT_CODE 43", "#   define PE_EXIT_CODE 1")
        (ws / "third_party" / "testlib" / "testlib.h").write_text(patched_testlib, encoding="utf-8")

        build_id = build_service.run_build(problem, self.user, prefer_local_solve_backend=True)
        row = db.fetch_one("SELECT status,artifact_path,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok", str(row["summary_json"] or ""))
        artifact_root = Path(str(row["artifact_path"] or ""))
        checker_bin = artifact_root / "bin" / "checker"
        self.assertTrue(checker_bin.exists(), f"checker binary missing: {checker_bin}")

        feedback = artifact_root / "logs" / "rc-check"
        feedback.mkdir(parents=True, exist_ok=True)
        test_in = artifact_root / "tests" / "001.in"
        test_ans = artifact_root / "ans" / "001.ans"
        team_out = feedback / "001.out"
        team_out.write_text("7\n", encoding="utf-8")
        result = run_cmd([str(checker_bin), str(test_in), str(test_ans), str(feedback)], stdin_path=team_out)
        self.assertEqual(result.returncode, 42, f"checker returned {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def test_repository_checker_forces_testlib_exit_codes_even_with_legacy_testlib_defaults(self) -> None:
        problem = self._owned_problem("repo-checker-rc")
        workspace_service.ensure_problem(problem, "Repo Checker RC")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "checkers",
            "validators",
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "checkers" / "checker.cpp").write_text(
            """#include "testlib.h"
int main(int argc, char** argv) {
    registerTestlibCmd(argc, argv);
    int p = ouf.readInt();
    int j = ans.readInt();
    if (p == j) quitf(_ok, "ok");
    quitf(_wa, "wa");
}
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")

        upstream_testlib = Path("third_party/upstream/testlib/testlib.h").read_text(encoding="utf-8", errors="replace")
        patched_testlib = upstream_testlib.replace("#   define OK_EXIT_CODE 42", "#   define OK_EXIT_CODE 0")
        patched_testlib = patched_testlib.replace("#   define WA_EXIT_CODE 43", "#   define WA_EXIT_CODE 1")
        patched_testlib = patched_testlib.replace("#   define PE_EXIT_CODE 43", "#   define PE_EXIT_CODE 1")
        (ws / "third_party" / "testlib" / "testlib.h").write_text(patched_testlib, encoding="utf-8")

        build_id = build_service.run_build(problem, self.user, prefer_local_solve_backend=True)
        row = db.fetch_one("SELECT status,artifact_path,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok", str(row["summary_json"] or ""))
        artifact_root = Path(str(row["artifact_path"] or ""))
        checker_bin = artifact_root / "bin" / "checker"
        self.assertTrue(checker_bin.exists(), f"checker binary missing: {checker_bin}")

        feedback = artifact_root / "logs" / "rc-check"
        feedback.mkdir(parents=True, exist_ok=True)
        test_in = artifact_root / "tests" / "001.in"
        test_ans = artifact_root / "ans" / "001.ans"
        team_out = feedback / "001.out"
        team_out.write_text("7\n", encoding="utf-8")
        result = run_cmd([str(checker_bin), str(test_in), str(test_ans), str(feedback)], stdin_path=team_out)
        self.assertEqual(result.returncode, 42, f"checker returned {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def test_build_succeeds_with_python_generator_from_tests_spec(self) -> None:
        problem = self._owned_problem("py-gen-build")
        workspace_service.ensure_problem(problem, "Py Gen Build")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "generators",
            "validators",
            "solutions",
            "tests/generator",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { long long x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "generators" / "gen.py").write_text("print('7')\n", encoding="utf-8")
        (ws / "tests" / "generator" / "001.in").write_text("gen\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [
                        {"id": "001", "kind": "gen", "sample": False},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        build_id = build_service.run_build(problem, self.user, prefer_local_solve_backend=True)
        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok", str(row["summary_json"] or ""))

    def test_build_fails_when_custom_sample_output_validation_fails(self) -> None:
        problem = self._owned_problem("sample-validate-fail")
        workspace_service.ensure_problem(problem, "Sample Validate Fail")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            "#include <iostream>\n"
            "int main(){ long long x=0; if(!(std::cin>>x)) return 0; std::cout<<x<<\"\\n\"; return 0; }\n",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [
                        {
                            "id": "001",
                            "kind": "manual",
                            "sample": True,
                            "sample_output": "999\n",
                            "sample_output_validate": True,
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        payload = json.loads(str(row["summary_json"] or "{}"))
        self.assertIn("sample custom output validation failed", str(payload.get("error") or ""))

    def test_build_skips_custom_sample_output_validation_when_disabled(self) -> None:
        problem = self._owned_problem("sample-validate-skip")
        workspace_service.ensure_problem(problem, "Sample Validate Skip")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "solutions",
            "tests/manual",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            "#include <iostream>\n"
            "int main(){ long long x=0; if(!(std::cin>>x)) return 0; std::cout<<x<<\"\\n\"; return 0; }\n",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [
                        {
                            "id": "001",
                            "kind": "manual",
                            "sample": True,
                            "sample_output": "999\n",
                            "sample_output_validate": False,
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok", str(row["summary_json"] or ""))

    def test_tests_spec_rejects_inline_payload_entries(self) -> None:
        payload = {
            "version": 2,
            "tests": [
                {"kind": "manual", "input": "1\n"},
            ],
        }
        with self.assertRaises(ValueError):
            loads_tests_spec(json.dumps(payload))

    def test_tests_spec_rejects_legacy_type_field_for_kind(self) -> None:
        payload = {
            "version": 2,
            "tests": [
                {"id": "001", "type": "manual", "sample": False},
            ],
        }
        with self.assertRaises(ValueError):
            loads_tests_spec(json.dumps(payload))

    def test_build_requires_explicit_accepted_solution_source_config(self) -> None:
        problem = self._owned_problem("require-main")
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
        problem = self._owned_problem("val-rc")
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

    def test_build_failure_reports_solve_returncode_and_stderr(self) -> None:
        problem = self._owned_problem("solve-rc")
        workspace_service.ensure_problem(problem, "Solve RC")
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
int main() { return 0; }
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
int main() { std::cerr << "solve boom"; return 7; }
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
        self.assertIn("accepted solution failed on 001.in (rc=7)", error_text)
        self.assertIn("stderr: solve boom", error_text)

    def test_build_solve_early_stops_on_first_failed_test(self) -> None:
        problem = self._owned_problem("solve-early-stop")
        workspace_service.ensure_problem(problem, "Solve Early Stop")
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
int main() { return 0; }
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
int main() { std::cerr << "solve boom"; return 7; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "manual" / "002.in").write_text("9\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,summary_json,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "failed")
        payload = json.loads(str(row["summary_json"] or "{}"))
        error_text = str(payload.get("error") or "")
        self.assertIn("accepted solution failed on 001.in (rc=7)", error_text)

        artifact_root = Path(str(row["artifact_path"] or ""))
        solve_log = (artifact_root / "logs" / "solve.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("early_stop=001.in", solve_log)
        self.assertNotIn("002.in: rc=", solve_log)
        self.assertFalse((artifact_root / "ans" / "002.ans").exists())

    def test_build_interactive_solve_uses_interactor_and_answer_payload(self) -> None:
        problem = self._owned_problem("interactive-solve")
        workspace_service.ensure_problem(problem, "Interactive Solve")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "checkers",
            "interactors",
            "solutions",
            "tests/manual",
            "tests/answers",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "mode": "interactive",
                    "time_limit_ms": 1000,
                    "memory_limit_mb": 512,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "interactor_source": "interactors/interactor.cpp",
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
int main() { return 0; }
""",
            encoding="utf-8",
        )
        (ws / "checkers" / "checker.cpp").write_text(
            """#include <iostream>
int main() { return 0; }
""",
            encoding="utf-8",
        )
        (ws / "interactors" / "interactor.cpp").write_text(
            """#include "testlib.h"
int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);
    int x = inf.readInt();
    std::printf("%d\\n", x);
    std::fflush(stdout);
    std::string token = ouf.readToken();
    if (token != "?") {
        quitf(_wa, "expected query token");
    }
    int asked = ouf.readInt();
    if (asked != x) {
        quitf(_wa, "asked=%d expected=%d", asked, x);
    }
    int hidden = ans.readInt();
    std::printf("%d\\n", hidden);
    std::fflush(stdout);
    int found = ouf.readInt();
    if (found != hidden) {
        quitf(_wa, "found=%d expected=%d", found, hidden);
    }
    tout << hidden << "\\n";
    quitf(_ok, "ok");
}
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() {
    int x = 0;
    if (!(std::cin >> x)) return 3;
    std::cout << "? " << x << std::endl;
    int y = 0;
    if (!(std::cin >> y)) return 4;
    std::cout << y << std::endl;
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "answers" / "001.ans").write_text("42\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [{"id": "001", "kind": "manual", "sample": True}],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "third_party" / "testlib" / "testlib.h").write_text(
            Path("third_party/upstream/testlib/testlib.h").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        artifact_root = Path(str(row["artifact_path"] or ""))
        self.assertEqual((artifact_root / "ans" / "001.ans").read_text(encoding="utf-8").strip(), "42")

    def test_build_multi_pass_interactive_preserves_imported_answer_payload(self) -> None:
        problem = self._owned_problem("multipass-imported-answers")
        workspace_service.ensure_problem(problem, "Multipass Imported Answers")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in [
            "config",
            "validators",
            "checkers",
            "interactors",
            "solutions",
            "tests/manual",
            "tests/answers",
            "third_party/testlib",
        ]:
            (ws / rel).mkdir(parents=True, exist_ok=True)

        (ws / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "mode": "multi-pass",
                    "time_limit_ms": 1000,
                    "memory_limit_mb": 512,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::ncmp.cpp",
                    "interactor_source": "interactors/interactor.cpp",
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
int main() { return 0; }
""",
            encoding="utf-8",
        )
        (ws / "interactors" / "interactor.cpp").write_text(
            """#include "testlib.h"
int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);
    quitf(_ok, "noop");
}
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { std::cout << "? 0\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "tests" / "answers" / "001.ans").write_text("42\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "tests": [{"id": "001", "kind": "manual", "sample": True}],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "third_party" / "testlib" / "testlib.h").write_text(
            Path("third_party/upstream/testlib/testlib.h").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        build_id = build_service.run_build(problem, self.user)
        row = db.fetch_one("SELECT status,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        artifact_root = Path(str(row["artifact_path"] or ""))
        self.assertEqual((artifact_root / "ans" / "001.ans").read_text(encoding="utf-8").strip(), "42")

    def test_build_solve_uses_active_judge_backend_for_answer_generation(self) -> None:
        problem = self._owned_problem("solve-jh")
        workspace_service.ensure_problem(problem, "Solve Judge Backend")
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
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

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}
                self.calls: list[list[str]] = []

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                self.calls.append(
                    {
                        "problem": problem,
                        "username": username,
                        "build_id": build_id,
                        "mode": mode,
                        "submission_path": submission_path,
                        "selected_tests": list(selected_tests or []),
                        "invocation_source": invocation_source,
                    }
                )
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                artifact_root = config.fs_manager.build_paths(str(build_row["build_ref"] or "")).root
                tests_root = artifact_root / "tests"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in list(selected_tests or []):
                    stem = Path(test_name).stem
                    payload = (tests_root / test_name).read_text(encoding="utf-8", errors="replace")
                    (run_root / f"{stem}.out").write_text(payload, encoding="utf-8")
                    tests.append({"test": test_name, "passes": [{"pass": 1, "verdict": "OK"}], "verdict": "OK"})
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps({"tests": tests, "error": ""}),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        artifact_root = Path(str(row["artifact_path"] or ""))
        self.assertEqual((artifact_root / "ans" / "001.ans").read_text(encoding="utf-8").strip(), "7")
        solve_log = (artifact_root / "logs" / "solve.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("solve_backend=domjudge-judgehost", solve_log)
        self.assertTrue(fake_jh.calls)
        self.assertEqual(str(fake_jh.calls[0].get("invocation_source") or ""), "build.solve")

    def test_build_solve_judge_backend_submits_single_task_for_all_tests(self) -> None:
        problem = self._owned_problem("solve-jh-single-task")
        workspace_service.ensure_problem(problem, "Solve Judge Backend Single Task")
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "manual" / "002.in").write_text("9\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}
                self.calls: list[dict[str, object]] = []

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                selected = list(selected_tests or [])
                self.calls.append(selected)
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                artifact_root = config.fs_manager.build_paths(str(build_row["build_ref"] or "")).root
                tests_root = artifact_root / "tests"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in selected:
                    stem = Path(test_name).stem
                    payload = (tests_root / test_name).read_text(encoding="utf-8", errors="replace")
                    (run_root / f"{stem}.out").write_text(payload, encoding="utf-8")
                    tests.append({"test": test_name, "passes": [{"pass": 1, "verdict": "OK"}], "verdict": "OK"})
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps({"tests": tests, "error": ""}),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        self.assertEqual(len(fake_jh.calls), 1)
        self.assertEqual(fake_jh.calls[0], ["001.in", "002.in"])
        self.assertEqual(len(set(fake_jh.calls[0])), 2)

    def test_build_solve_judge_backend_keeps_single_task_when_solve_jobs_gt_one(self) -> None:
        problem = self._owned_problem("solve-jh-split")
        workspace_service.ensure_problem(problem, "Solve Judge Backend Split Tasks")
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
                    "solve_jobs": 3,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        for idx in range(1, 6):
            (ws / "tests" / "manual" / f"{idx:03d}.in").write_text(f"{idx}\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}
                self.calls: list[dict[str, object]] = []

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                selected = list(selected_tests or [])
                self.calls.append(
                    {
                        "selected_tests": selected,
                        "invocation_id": str(invocation_id or ""),
                        "invocation_run_ids": [str(item or "") for item in list(invocation_run_ids or [])],
                    }
                )
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                artifact_root = config.fs_manager.build_paths(str(build_row["build_ref"] or "")).root
                tests_root = artifact_root / "tests"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in selected:
                    stem = Path(test_name).stem
                    payload = (tests_root / test_name).read_text(encoding="utf-8", errors="replace")
                    (run_root / f"{stem}.out").write_text(payload, encoding="utf-8")
                    tests.append({"test": test_name, "passes": [{"pass": 1, "verdict": "OK"}], "verdict": "OK"})
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps({"tests": tests, "error": ""}),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        self.assertEqual(len(fake_jh.calls), 1)
        selected = [str(item or "") for item in list(fake_jh.calls[0].get("selected_tests") or [])]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(set(selected), {f"{idx:03d}.in" for idx in range(1, 6)})
        invocation_id = str(fake_jh.calls[0].get("invocation_id") or "")
        self.assertTrue(invocation_id.startswith(f"inv-buildsolve-{build_id}-"))
        declared_run_ids = [str(item or "") for item in list(fake_jh.calls[0].get("invocation_run_ids") or []) if str(item or "")]
        self.assertEqual(len(declared_run_ids), 1)
        for token in declared_run_ids:
            self.assertTrue(token.startswith("r-buildsolve-"))

    def test_build_solve_judge_backend_early_stops_on_first_failed_test(self) -> None:
        problem = self._owned_problem("solve-jh-early-stop")
        workspace_service.ensure_problem(problem, "Solve Judge Backend Early Stop")
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            """#include <iostream>
int main() { int x = 0; if (!(std::cin >> x)) return 0; std::cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        (ws / "tests" / "manual" / "002.in").write_text("9\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}
                self.calls: list[list[str]] = []

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                selected = list(selected_tests or [])
                self.calls.append(selected)
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in selected:
                    if test_name == "001.in":
                        tests.append(
                            {
                                "test": test_name,
                                "passes": [{"pass": 1, "verdict": "WA", "feedback": "first test failed"}],
                                "verdict": "WA",
                            }
                        )
                    else:
                        (run_root / f"{Path(test_name).stem}.out").write_text("ok\n", encoding="utf-8")
                        tests.append(
                            {
                                "test": test_name,
                                "passes": [{"pass": 1, "verdict": "OK"}],
                                "verdict": "OK",
                            }
                        )
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps({"tests": tests, "error": ""}),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status,summary_json,artifact_path FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = json.loads(str(row["summary_json"] or "{}"))
        self.assertIn("accepted solution failed on 001.in", str(summary.get("error") or ""))
        self.assertEqual(str(summary.get("failed_test") or ""), "001.in")
        solve_log = (Path(str(row["artifact_path"] or "")) / "logs" / "solve.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("early_stop=001.in", solve_log)
        self.assertNotIn("002.in: rc=", solve_log)
        self.assertEqual(len(fake_jh.calls), 1)
        self.assertEqual(fake_jh.calls[0][0], "001.in")

    def test_build_solve_judge_backend_requires_ok_verdict_before_accepting_answer(self) -> None:
        problem = self._owned_problem("solve-jh-verdict")
        workspace_service.ensure_problem(problem, "Solve Judge Verdict")
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
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

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                artifact_root = config.fs_manager.build_paths(str(build_row["build_ref"] or "")).root
                tests_root = artifact_root / "tests"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in list(selected_tests or []):
                    stem = Path(test_name).stem
                    payload = (tests_root / test_name).read_text(encoding="utf-8", errors="replace")
                    # Keep output materialized to assert build.solve does not accept output-only success.
                    (run_root / f"{stem}.out").write_text(payload, encoding="utf-8")
                    tests.append(
                        {
                            "test": test_name,
                            "passes": [{"pass": 1, "verdict": "WA", "feedback": "judge mismatch"}],
                            "verdict": "WA",
                        }
                    )
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps({"tests": tests, "error": ""}),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = json.loads(str(row["summary_json"] or "{}"))
        self.assertIn("accepted solution failed on 001.in", str(summary.get("error") or ""))
        self.assertIn("judge mismatch", str(summary.get("error") or ""))

    def test_build_solve_judge_backend_ce_surfaces_compile_diagnostics(self) -> None:
        problem = self._owned_problem("solve-jh-ce")
        workspace_service.ensure_problem(problem, "Solve Judge CE Diagnostics")
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
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "checkers" / "checker.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("7\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        class _FakeInvocationBackend:
            @staticmethod
            def active_backend_name() -> str:
                return "domjudge-judgehost"

        class _FakeJudgehost:
            def __init__(self) -> None:
                self._tasks: dict[str, str] = {}

            @staticmethod
            def enabled() -> bool:
                return True

            @staticmethod
            def auth_token_configured() -> bool:
                return True

            def enqueue_task(
                self,
                *,
                problem: str,
                username: str,
                build_id: str,
                mode: str,
                submission_path: str | None,
                upload_content: bytes | None,
                upload_filename: str | None,
                run_id: str,
                selected_tests: list[str] | None,
                invocation_id: str,
                invocation_run_ids: list[str] | None,
                expected_behavior: str,
                invocation_source: str,
            ) -> str:
                task_id = f"jt-fake-{uuid.uuid4().hex[:8]}"
                run_root = config.fs_manager.prepare_run_root(run_id)
                run_root.mkdir(parents=True, exist_ok=True)
                tests: list[dict[str, object]] = []
                for test_name in list(selected_tests or []):
                    tests.append(
                        {
                            "test": test_name,
                            "passes": [{"pass": 1, "verdict": "CE"}],
                            "verdict": "CE",
                        }
                    )
                summary = {
                    "tests": tests,
                    "error": "",
                    "compile_diagnostics": [
                        {
                            "level": "error",
                            "message": "toolchain parse failed",
                            "file": "accepted.cpp",
                            "line": 12,
                            "column": 4,
                        }
                    ],
                }
                ctx = workspace_service.workspace_context(problem, username, include_recent=False)
                build_row = db.fetch_one("SELECT build_ref FROM builds WHERE id=?", [build_id])
                if build_row is None:
                    raise RuntimeError("build row missing")
                db.execute(
                    """
                    INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        run_id,
                        int(ctx["problem"]["id"]),
                        int(ctx["workspace"]["id"]),
                        build_id,
                        str(build_row["build_ref"] or ""),
                        str(mode or "pass-fail"),
                        "ok",
                        json.dumps(summary),
                        str(run_root),
                        "2026-03-02T00:00:00Z",
                        "2026-03-02T00:00:01Z",
                    ],
                )
                self._tasks[task_id] = run_id
                return task_id

            def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
                return str(self._tasks.get(task_id) or "")

        fake_inv = _FakeInvocationBackend()
        fake_jh = _FakeJudgehost()
        old_inv = getattr(build_service, "_invocation_backend_service", None)
        old_jh = getattr(build_service, "_judgehost_task_service", None)
        try:
            build_service.bind_runtime_services(
                invocation_backend_service=fake_inv,  # type: ignore[arg-type]
                judgehost_task_service=fake_jh,  # type: ignore[arg-type]
            )
            build_id = build_service.run_build(problem, self.user)
        finally:
            build_service.bind_runtime_services(
                invocation_backend_service=old_inv,
                judgehost_task_service=old_jh,
            )

        row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = json.loads(str(row["summary_json"] or "{}"))
        error_text = str(summary.get("error") or "")
        self.assertIn("accepted solution failed on 001.in", error_text)
        self.assertIn("accepted.cpp:12:4: toolchain parse failed", error_text)

    def test_build_cache_key_generation_digest_mismatch_returns_miss(self) -> None:
        problem = self._owned_problem("cache-key-digest")
        workspace_service.ensure_problem(problem, "Cache Key Digest")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in ["config", "validators", "solutions", "tests/manual", "third_party/testlib"]:
            (ws / rel).mkdir(parents=True, exist_ok=True)
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "compile_jobs": 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text(
            "#include <iostream>\nint main(){int x=0; if(!(std::cin>>x)) return 0; std::cout<<x<<\"\\n\"; return 0;}\n",
            encoding="utf-8",
        )
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        build_id = build_service.run_build(problem, self.user, prefer_local_solve_backend=True)
        row = db.fetch_one(
            "SELECT status,source_commit,source_ref,problem_id,workspace_id FROM builds WHERE id=?",
            [build_id],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        source_commit = str(row["source_commit"] or "").strip()
        source_ref = str(row["source_ref"] or "").strip()
        problem_id = int(row["problem_id"])
        workspace_id = int(row["workspace_id"])
        generation_digest = build_service._generation_params_digest(ws, sample_only=False)
        toolchain_digest = build_service._toolchain_cmd_digest()

        hit = build_service._cached_build_id_for_source(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=source_commit,
            source_ref=source_ref,
            generation_params_digest=generation_digest,
            toolchain_cmd_digest=toolchain_digest,
            sample_only=False,
        )
        self.assertIn(hit, {"", build_id})

        miss = build_service._cached_build_id_for_source(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=source_commit,
            source_ref=source_ref,
            generation_params_digest=("0" * 64),
            toolchain_cmd_digest=toolchain_digest,
            sample_only=False,
        )
        self.assertEqual(miss, "")

    def test_build_join_returns_failed_business_message_when_upstream_failed(self) -> None:
        problem = self._owned_problem("build-join-fail")
        workspace_service.ensure_problem(problem, "Build Join Fail")
        workspace_service.grant_repo_access(problem, self.user, "owner")
        ws = Path(workspace_service.ensure_workspace(problem, self.user))

        for rel in ["config", "validators", "solutions", "tests/manual", "third_party/testlib"]:
            (ws / rel).mkdir(parents=True, exist_ok=True)
        (ws / "config" / "build.json").write_text(
            json.dumps(
                {
                    "validator_source": "validators/validator.cpp",
                    "checker_standard": "std::wcmp.cpp",
                    "accepted_solution_source": "solutions/accepted.cpp",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (ws / "validators" / "validator.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        testlib = ws / "third_party" / "testlib" / "testlib.h"
        if not testlib.exists():
            testlib.write_text("// testlib placeholder\n", encoding="utf-8")

        run_cmd(["git", "-C", str(ws), "add", "-A"])
        run_cmd(["git", "-C", str(ws), "commit", "-m", f"seed-upstream-{uuid.uuid4().hex[:6]}"])
        head = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        resolved = workspace_service.resolve_commit(ws, head)
        snapshot = workspace_service.create_snapshot(ws, resolved)
        try:
            generation_digest = build_service._generation_params_digest(snapshot, sample_only=False)
        finally:
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        toolchain_digest = build_service._toolchain_cmd_digest()
        ctx = workspace_service.workspace_context(problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        cache_key = build_service._build_cache_key(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=resolved,
            source_ref=head,
            generation_params_digest=generation_digest,
            toolchain_cmd_digest=toolchain_digest,
            sample_only=False,
        )
        cache_key_hash = build_service._build_cache_key_hash(cache_key)
        upstream_build_id = self.random_id("b-upstream-failed")
        upstream_root = self._artifact_root(upstream_build_id)
        upstream_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                upstream_build_id,
                problem_id,
                workspace_id,
                resolved,
                head,
                "failed",
                json.dumps({"error": "upstream failed"}),
                str(upstream_root),
                "2026-03-05T00:00:00Z",
                "2026-03-05T00:00:01Z",
            ],
        )
        with build_service._build_inflight_lock:
            build_service._build_inflight[cache_key_hash] = upstream_build_id
        try:
            with self.assertRaisesRegex(RuntimeError, "same-configuration build already failed"):
                build_service.run_build(problem, self.user, commit=head)
        finally:
            with build_service._build_inflight_lock:
                build_service._build_inflight.pop(cache_key_hash, None)

    def test_cached_build_lookup_deletes_bad_key_on_invalid_hit(self) -> None:
        cache_service = build_service._async_task_cache_service
        self.assertIsNotNone(cache_service)
        assert cache_service is not None
        key = build_service._build_cache_key(
            problem_id=1,
            workspace_id=1,
            source_commit="a" * 40,
            source_ref="main",
            generation_params_digest="1" * 64,
            toolchain_cmd_digest="2" * 64,
            sample_only=False,
        )
        cache_service.put(build_service.BUILD_CACHE_NAMESPACE, key, {"build_id": "b-missing"})
        miss = build_service._cached_build_id_for_source(
            problem_id=1,
            workspace_id=1,
            source_commit="a" * 40,
            source_ref="main",
            generation_params_digest="1" * 64,
            toolchain_cmd_digest="2" * 64,
            sample_only=False,
        )
        self.assertEqual(miss, "")
        self.assertIsNone(cache_service.get(build_service.BUILD_CACHE_NAMESPACE, key))





