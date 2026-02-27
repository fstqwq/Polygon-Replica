from __future__ import annotations

import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.common import SmokeBase
from app.impl.config import config
from app.services.util import run_cmd

db = config.db
run_service = config.run_service
toolchain_service = config.toolchain_service
workspace_service = config.workspace_service


class TestRun(SmokeBase):
    def test_run_service_and_solution_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "solutions").is_dir())
        self.assertTrue(callable(run_service.run_submission))

    def test_validator_style_verdict_supports_icpc_exit_codes(self) -> None:
        self.assertEqual(run_service._validator_style_verdict(42), "OK")
        self.assertEqual(run_service._validator_style_verdict(43), "WA")
        self.assertEqual(run_service._validator_style_verdict(0), "OK")
        self.assertEqual(run_service._validator_style_verdict(1), "WA")

    def test_standard_checker_supports_icpc_output_validator_cli(self) -> None:
        checker_src = Path("third_party/upstream/testlib/checkers/wcmp.cpp")
        with TemporaryDirectory() as build_tmp:
            checker_bin = Path(build_tmp) / "wcmp"
            compile_res = run_cmd(
                [
                    "g++",
                    "-O2",
                    "-std=c++20",
                    "-pipe",
                    "-Ithird_party/upstream/testlib",
                    str(checker_src),
                    "-o",
                    str(checker_bin),
                ]
            )
            self.assertEqual(
                compile_res.returncode,
                0,
                f"standard checker compile failed\nstdout:\n{compile_res.stdout}\nstderr:\n{compile_res.stderr}",
            )

            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                inf = root / "001.in"
                ans = root / "001.ans"
                out_ok = root / "001.out"
                out_wa = root / "002.out"
                feedback_dir = root / "feedback"
                feedback_dir.mkdir(parents=True, exist_ok=True)
                inf.write_text("1\n", encoding="utf-8")
                ans.write_text("1\n", encoding="utf-8")
                out_ok.write_text("1\n", encoding="utf-8")
                out_wa.write_text("2\n", encoding="utf-8")

                ok_res = run_cmd([str(checker_bin), str(inf), str(ans), str(feedback_dir)], stdin_path=out_ok)
                self.assertEqual(ok_res.returncode, 42, ok_res.stderr)
                judge_msg = feedback_dir / "judgemessage.txt"
                self.assertTrue(judge_msg.exists())

                wa_res = run_cmd([str(checker_bin), str(inf), str(ans), str(feedback_dir)], stdin_path=out_wa)
                self.assertEqual(wa_res.returncode, 43, wa_res.stderr)
                self.assertIn("found", judge_msg.read_text(encoding="utf-8", errors="replace"))

    def test_run_submission_can_filter_selected_tests(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "ac.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    string s;
    if (!(cin >> s)) return 0;
    cout << s << "\\n";
    return 0;
}
""",
            encoding="utf-8",
        )

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-select-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        (artifact_root / "tests" / "001.in").write_text("one\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("one\n", encoding="utf-8")
        (artifact_root / "tests" / "002.in").write_text("two\n", encoding="utf-8")
        (artifact_root / "ans" / "002.ans").write_text("two\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 1,
                    "run_jobs": 1,
                    "time_limit_ms": 200,
                    "run_memory_mb": 512,
                    "run_process_limit": 64,
                    "run_output_kb": 16384,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

        checker_src = Path("third_party/upstream/testlib/checkers/wcmp.cpp")
        checker_bin = artifact_root / "bin" / "checker"
        ok, cout, cerr, _ = toolchain_service.compile_cpp(
            checker_src,
            checker_bin,
            include_dirs=[Path("third_party/upstream/testlib")],
            path_roots=[Path("."), Path("third_party/upstream/testlib")],
        )
        self.assertTrue(ok, msg=f"standard checker compile failed\nstdout:\n{cout}\nstderr:\n{cerr}")

        run_id = run_service.run_submission(
            self.problem,
            self.user,
            build_id,
            submission_path="solutions/ac.cpp",
            mode="pass-fail",
            selected_tests=["002.in"],
        )
        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("test") or ""), "002.in")
        self.assertEqual(summary.get("selected_tests"), ["002.in"])
        self.assertEqual(int(summary.get("selected_tests_count") or 0), 1)

    def test_standard_checker_reports_ok_wa_re_tle_verdicts(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "ac.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    string s;
    if (!(cin >> s)) return 0;
    cout << s << "\\n";
    return 0;
}
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "wa.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() { cout << "wrong" << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "re.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() {
    int* p = nullptr;
    return *p;
}
""",
            encoding="utf-8",
        )
        (ws / "solutions" / "tle.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() {
    while (true) {}
}
""",
            encoding="utf-8",
        )

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-verdict-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        (artifact_root / "tests" / "001.in").write_text("hello\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("hello\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 1,
                    "run_jobs": 1,
                    "time_limit_ms": 200,
                    "run_memory_mb": 512,
                    "run_process_limit": 64,
                    "run_output_kb": 16384,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        checker_src = Path("third_party/upstream/testlib/checkers/wcmp.cpp")
        checker_bin = artifact_root / "bin" / "checker"
        ok, cout, cerr, _ = toolchain_service.compile_cpp(
            checker_src,
            checker_bin,
            include_dirs=[Path("third_party/upstream/testlib")],
            path_roots=[Path("."), Path("third_party/upstream/testlib")],
        )
        self.assertTrue(ok, msg=f"standard checker compile failed\nstdout:\n{cout}\nstderr:\n{cerr}")
        self.assertTrue(checker_bin.exists())

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

        def _run_and_first_test(source_rel: str) -> dict:
            run_id = run_service.run_submission(
                self.problem,
                self.user,
                build_id,
                submission_path=source_rel,
                mode="pass-fail",
            )
            row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
            self.assertIsNotNone(row)
            self.assertEqual(str(row["status"]), "ok")
            summary = json.loads(str(row["summary_json"] or "{}"))
            tests = summary.get("tests")
            self.assertIsInstance(tests, list)
            self.assertTrue(tests)
            first = tests[0]
            self.assertIsInstance(first, dict)
            return first

        self.assertEqual(str(_run_and_first_test("solutions/ac.cpp").get("verdict") or ""), "OK")
        self.assertEqual(str(_run_and_first_test("solutions/wa.cpp").get("verdict") or ""), "WA")
        self.assertEqual(str(_run_and_first_test("solutions/re.cpp").get("verdict") or ""), "RE")
        tle_row = _run_and_first_test("solutions/tle.cpp")
        self.assertEqual(str(tle_row.get("verdict") or ""), "TL")
        # Effective timeout rule: max(time_limit_ms * 2, time_limit_ms + 1000).
        self.assertLessEqual(int(tle_row.get("time_ms") or 0), 1200)

    def test_nonzero_exit_over_timeout_budget_is_classified_as_tle(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "probe.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() { cout << 1 << "\\n"; return 0; }
""",
            encoding="utf-8",
        )

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-timeout-map-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        (artifact_root / "tests" / "001.in").write_text("1\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("1\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 1,
                    "run_jobs": 1,
                    "time_limit_ms": 1000,
                    "run_memory_mb": 512,
                    "run_process_limit": 64,
                    "run_output_kb": 64,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

        with patch.object(run_service, "_run_pass", return_value=(1, 2392, 0)):
            run_id = run_service.run_submission(
                self.problem,
                self.user,
                build_id,
                submission_path="solutions/probe.cpp",
                mode="pass-fail",
            )

        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertTrue(tests)
        first = tests[0] if isinstance(tests[0], dict) else {}
        self.assertEqual(str(first.get("verdict") or ""), "TL")
        self.assertEqual(int(first.get("time_ms") or 0), 2000)
