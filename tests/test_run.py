from __future__ import annotations

import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.common import SmokeBase
from app.impl.config import config
from app.runtime_values import build_runtime_values
from app.services.util import run_cmd

db = config.db
run_service = config.run_service
toolchain_service = config.toolchain_service
workspace_service = config.workspace_service


class TestRun(SmokeBase):
    def _build_artifact_root(self, build_id: str) -> tuple[str, Path]:
        build_ref = config.fs_manager.compute_build_ref(
            {"suite": "run", "problem": self.problem, "build_id": str(build_id or "").strip()}
        )
        artifact_root = config.fs_manager.ensure_build_layout(build_ref).root.resolve()
        return build_ref, artifact_root

    def test_run_service_and_solution_layout(self) -> None:
        ws = self._workspace_path()
        self.assertTrue((ws / "solutions").is_dir())
        self.assertTrue(callable(run_service.run_submission))

    def test_effective_run_timeout_formula_is_mode_aware(self) -> None:
        old_pass = int(run_service.wall_time_slack_pass_fail_sec)
        old_multi = int(run_service.wall_time_slack_multi_pass_sec)
        old_interactive = int(run_service.wall_time_slack_interactive_sec)
        try:
            run_service.wall_time_slack_pass_fail_sec = 1
            run_service.wall_time_slack_multi_pass_sec = 15
            run_service.wall_time_slack_interactive_sec = 15
            self.assertEqual(run_service._effective_run_timeout_ms(1000, mode="pass-fail"), 3000)
            self.assertEqual(run_service._effective_run_timeout_ms(1000, mode="multi-pass"), 17000)
            self.assertEqual(run_service._effective_run_timeout_ms(1000, mode="interactive"), 17000)
        finally:
            run_service.wall_time_slack_pass_fail_sec = old_pass
            run_service.wall_time_slack_multi_pass_sec = old_multi
            run_service.wall_time_slack_interactive_sec = old_interactive

    def test_run_timeout_hot_reload_clears_cached_run_config(self) -> None:
        try:
            with TemporaryDirectory() as tmp:
                artifact_root = Path(tmp)
                (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
                (artifact_root / "logs" / "run_config.json").write_text(
                    json.dumps(
                        {
                            "time_limit_ms": 1000,
                            "mode": "pass-fail",
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                baseline = run_service._load_run_config(artifact_root, default_mode="pass-fail")
                self.assertEqual(int(baseline.get("run_timeout_ms") or 0), 2000 + int(run_service.wall_time_slack_pass_fail_sec) * 1000)
                run_service.apply_runtime_values(
                    build_runtime_values(
                        {
                            "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC": 9,
                            "RUN_WALL_TIME_SLACK_MULTI_PASS_SEC": int(run_service.wall_time_slack_multi_pass_sec),
                            "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC": int(run_service.wall_time_slack_interactive_sec),
                        }
                    )
                )
                reloaded = run_service._load_run_config(artifact_root, default_mode="pass-fail")
                self.assertEqual(int(reloaded.get("run_timeout_ms") or 0), 11000)
        finally:
            run_service.apply_runtime_values(config.constants)

    def test_standard_checker_supports_icpc_output_validator_cli(self) -> None:
        checker_src = Path("third_party/upstream/testlib/checkers/wcmp.cpp")
        with TemporaryDirectory() as build_tmp:
            checker_bin = Path(build_tmp) / "wcmp"
            compile_res = run_cmd(
                [
                    "g++",
                    "-O2",
                    "-std=gnu++20",
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
        build_ref, artifact_root = self._build_artifact_root(build_id)
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
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

    def test_run_submission_fl_skips_following_tests(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "probe.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-fl-skip-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        for name in ("001", "002", "003"):
            (artifact_root / "tests" / f"{name}.in").write_text(f"{name}\n", encoding="utf-8")
            (artifact_root / "ans" / f"{name}.ans").write_text(f"{name}\n", encoding="utf-8")
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )

        def _fake_run_noninteractive(*_args, **kwargs):
            test_path = kwargs.get("test")
            if not isinstance(test_path, Path):
                test_path = _args[10] if len(_args) > 10 else Path("001.in")
            if test_path.name == "001.in":
                return {
                    "test": "001.in",
                    "passes": [{"pass": 1, "verdict": "FL", "feedback": "boom", "time_ms": 0, "time_user_ms": 0, "time_wall_ms": 0, "memory_kb": 0}],
                    "verdict": "FL",
                    "sandbox_status": "fail",
                    "time_ms": 0,
                    "time_user_ms": 0,
                    "time_wall_ms": 0,
                    "memory_kb": 0,
                    "feedback_files": [],
                    "message": "boom",
                }
            return {
                "test": test_path.name,
                "passes": [{"pass": 1, "verdict": "OK", "time_ms": 0, "time_user_ms": 0, "time_wall_ms": 0, "memory_kb": 0}],
                "verdict": "OK",
                "sandbox_status": "ok",
                "time_ms": 0,
                "time_user_ms": 0,
                "time_wall_ms": 0,
                "memory_kb": 0,
                "feedback_files": [],
            }

        with (
            patch.object(run_service.toolchain, "compile_program", return_value=(True, "", "", "test-digest")),
            patch.object(run_service, "_run_noninteractive_test", side_effect=_fake_run_noninteractive) as mocked_run,
        ):
            run_id = run_service.run_submission(
                self.problem,
                self.user,
                build_id,
                submission_path="solutions/probe.cpp",
                mode="pass-fail",
            )
        self.assertEqual(mocked_run.call_count, 1)
        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 3)
        first = tests[0] if isinstance(tests[0], dict) else {}
        second = tests[1] if isinstance(tests[1], dict) else {}
        third = tests[2] if isinstance(tests[2], dict) else {}
        self.assertEqual(str(first.get("test") or ""), "001.in")
        self.assertEqual(str(first.get("verdict") or "").upper(), "FL")
        self.assertEqual(str(second.get("test") or ""), "002.in")
        self.assertEqual(str(second.get("verdict") or "").upper(), "FL")
        self.assertIn("fail due to test 001.in", str(second.get("message") or ""))
        self.assertEqual(str(third.get("test") or ""), "003.in")
        self.assertEqual(str(third.get("verdict") or "").upper(), "FL")
        self.assertIn("fail due to test 001.in", str(third.get("message") or ""))

    def test_run_submission_parallel_fl_masks_following_tests(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "probe.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-fl-mask-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        for name in ("001", "002", "003"):
            (artifact_root / "tests" / f"{name}.in").write_text(f"{name}\n", encoding="utf-8")
            (artifact_root / "ans" / f"{name}.ans").write_text(f"{name}\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 1,
                    "run_jobs": 3,
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )

        def _fake_run_noninteractive(*_args, **kwargs):
            test_path = kwargs.get("test")
            if not isinstance(test_path, Path):
                test_path = _args[10] if len(_args) > 10 else Path("001.in")
            verdict = "OK"
            message = ""
            if test_path.name == "002.in":
                verdict = "FL"
                message = "checker crashed"
            pass_row = {
                "pass": 1,
                "verdict": verdict,
                "time_ms": 0,
                "time_user_ms": 0,
                "time_wall_ms": 0,
                "memory_kb": 0,
            }
            if message:
                pass_row["feedback"] = message
            row = {
                "test": test_path.name,
                "passes": [pass_row],
                "verdict": verdict,
                "sandbox_status": "fail" if verdict == "FL" else "ok",
                "time_ms": 0,
                "time_user_ms": 0,
                "time_wall_ms": 0,
                "memory_kb": 0,
                "feedback_files": [],
            }
            if message:
                row["message"] = message
            return row

        with (
            patch.object(run_service.toolchain, "compile_program", return_value=(True, "", "", "test-digest")),
            patch.object(run_service, "_run_noninteractive_test", side_effect=_fake_run_noninteractive),
        ):
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
        self.assertEqual(len(tests), 3)
        first = tests[0] if isinstance(tests[0], dict) else {}
        second = tests[1] if isinstance(tests[1], dict) else {}
        third = tests[2] if isinstance(tests[2], dict) else {}
        self.assertEqual(str(first.get("verdict") or "").upper(), "OK")
        self.assertEqual(str(second.get("verdict") or "").upper(), "FL")
        self.assertEqual(str(third.get("verdict") or "").upper(), "FL")
        self.assertIn("fail due to test 002.in", str(third.get("message") or ""))

    def test_running_summary_persistence_is_event_throttled(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "probe.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-progress-event-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        for idx in range(1, 6):
            stem = f"{idx:03d}"
            (artifact_root / "tests" / f"{stem}.in").write_text(f"{idx}\n", encoding="utf-8")
            (artifact_root / "ans" / f"{stem}.ans").write_text(f"{idx}\n", encoding="utf-8")
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-03-05T00:00:00Z",
                "2026-03-05T00:00:01Z",
            ],
        )

        def _fake_run_noninteractive(*_args, **kwargs):
            test_path = kwargs.get("test")
            if not isinstance(test_path, Path):
                test_path = _args[10] if len(_args) > 10 else Path("001.in")
            return {
                "test": test_path.name,
                "passes": [{"pass": 1, "verdict": "OK", "time_ms": 0, "time_user_ms": 0, "time_wall_ms": 0, "memory_kb": 0}],
                "verdict": "OK",
                "sandbox_status": "ok",
                "time_ms": 0,
                "time_user_ms": 0,
                "time_wall_ms": 0,
                "memory_kb": 0,
                "feedback_files": [],
            }

        old_interval = int(run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS)
        old_batch = int(run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES)
        try:
            run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = 10_000_000
            run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES = 2
            with (
                patch.object(run_service.toolchain, "compile_program", return_value=(True, "", "", "test-digest")),
                patch.object(run_service, "_run_noninteractive_test", side_effect=_fake_run_noninteractive),
                patch.object(db, "execute", wraps=db.execute) as mocked_execute,
            ):
                run_id = run_service.run_submission(
                    self.problem,
                    self.user,
                    build_id,
                    submission_path="solutions/probe.cpp",
                    mode="pass-fail",
                )
        finally:
            run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = old_interval
            run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES = old_batch

        running_summary_updates = [
            call
            for call in mocked_execute.call_args_list
            if call.args and str(call.args[0]).strip() == "UPDATE runs SET summary_json=? WHERE id=?"
        ]
        self.assertEqual(len(running_summary_updates), 3)

        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 5)

    def test_running_summary_persistence_is_time_throttled(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "probe.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-progress-time-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        for idx in range(1, 4):
            stem = f"{idx:03d}"
            (artifact_root / "tests" / f"{stem}.in").write_text(f"{idx}\n", encoding="utf-8")
            (artifact_root / "ans" / f"{stem}.ans").write_text(f"{idx}\n", encoding="utf-8")
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-03-05T00:00:00Z",
                "2026-03-05T00:00:01Z",
            ],
        )

        def _fake_run_noninteractive(*_args, **kwargs):
            test_path = kwargs.get("test")
            if not isinstance(test_path, Path):
                test_path = _args[10] if len(_args) > 10 else Path("001.in")
            return {
                "test": test_path.name,
                "passes": [{"pass": 1, "verdict": "OK", "time_ms": 0, "time_user_ms": 0, "time_wall_ms": 0, "memory_kb": 0}],
                "verdict": "OK",
                "sandbox_status": "ok",
                "time_ms": 0,
                "time_user_ms": 0,
                "time_wall_ms": 0,
                "memory_kb": 0,
                "feedback_files": [],
            }

        monotonic_samples = iter([0.0, 0.1, 1.0])

        def _fake_monotonic() -> float:
            try:
                return next(monotonic_samples)
            except StopIteration:
                return 1.0

        old_interval = int(run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS)
        old_batch = int(run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES)
        try:
            run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = 500
            run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES = 100
            with (
                patch.object(run_service.toolchain, "compile_program", return_value=(True, "", "", "test-digest")),
                patch.object(run_service, "_run_noninteractive_test", side_effect=_fake_run_noninteractive),
                patch("app.services.run_service.time.monotonic", side_effect=_fake_monotonic),
                patch.object(db, "execute", wraps=db.execute) as mocked_execute,
            ):
                run_id = run_service.run_submission(
                    self.problem,
                    self.user,
                    build_id,
                    submission_path="solutions/probe.cpp",
                    mode="pass-fail",
                )
        finally:
            run_service.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = old_interval
            run_service.RUN_PROGRESS_PERSIST_BATCH_UPDATES = old_batch

        running_summary_updates = [
            call
            for call in mocked_execute.call_args_list
            if call.args and str(call.args[0]).strip() == "UPDATE runs SET summary_json=? WHERE id=?"
        ]
        self.assertEqual(len(running_summary_updates), 2)

        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 3)

    def test_run_submission_build_failure_emits_synthetic_fl_test_detail(self) -> None:
        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"b-run-build-fail-{uuid.uuid4().hex[:8]}"
        build_ref, build_root = self._build_artifact_root(build_id)
        build_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "failed",
                json.dumps(
                    {
                        "error": "accepted solution failed on 001.in",
                        "failed_step": "solve",
                        "failed_test": "001.in",
                    }
                ),
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

        run_id = run_service.run_submission(
            self.problem,
            self.user,
            build_id,
            submission_path="solutions/jly.cpp",
            mode="pass-fail",
        )
        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertEqual(len(tests), 1)
        first = tests[0] if isinstance(tests[0], dict) else {}
        self.assertEqual(str(first.get("test") or ""), "001.in")
        self.assertEqual(str(first.get("verdict") or "").upper(), "FL")
        passes = first.get("passes")
        self.assertIsInstance(passes, list)
        self.assertTrue(passes)
        pass_row = passes[0] if isinstance(passes[0], dict) else {}
        self.assertEqual(str(pass_row.get("verdict") or "").upper(), "FL")
        self.assertIn("accepted solution failed on 001.in", str(pass_row.get("feedback") or ""))

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
        build_ref, artifact_root = self._build_artifact_root(build_id)
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
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
        wa_row = _run_and_first_test("solutions/wa.cpp")
        self.assertEqual(str(wa_row.get("verdict") or ""), "WA")
        wa_passes = wa_row.get("passes")
        self.assertIsInstance(wa_passes, list)
        self.assertTrue(wa_passes)
        wa_first_pass = wa_passes[0] if isinstance(wa_passes[0], dict) else {}
        self.assertTrue(str(wa_first_pass.get("feedback") or "").strip())
        self.assertEqual(str(_run_and_first_test("solutions/re.cpp").get("verdict") or ""), "RE")
        tle_row = _run_and_first_test("solutions/tle.cpp")
        self.assertEqual(str(tle_row.get("verdict") or ""), "TL")
        # Effective timeout rule (pass-fail): 2 * TL + 1s wall slack.
        self.assertLessEqual(int(tle_row.get("time_ms") or 0), 1400)

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
        build_ref, artifact_root = self._build_artifact_root(build_id)
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
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
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

        with patch.object(run_service, "_run_pass", return_value=(1, 3392, 3392, 0)):
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
        self.assertEqual(int(first.get("time_ms") or 0), 3000)
        self.assertEqual(int(first.get("time_user_ms") or 0), 3000)
        self.assertGreaterEqual(int(first.get("time_wall_ms") or 0), 3000)

    def test_multi_pass_with_interactor_advances_passes_until_checker_ok(self) -> None:
        ws = Path(self._workspace_path())
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "ac.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    string prompt;
    if (!(cin >> prompt)) return 0;
    if (prompt == "P1") {
        cout << "hello" << endl;
        return 0;
    }
    if (prompt == "P2") {
        cout << "42" << endl;
        return 0;
    }
    return 0;
}
""",
            encoding="utf-8",
        )

        ctx = workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = f"b-run-mp-itr-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._build_artifact_root(build_id)
        (artifact_root / "tests").mkdir(parents=True, exist_ok=True)
        (artifact_root / "ans").mkdir(parents=True, exist_ok=True)
        (artifact_root / "bin").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)

        (artifact_root / "tests" / "001.in").write_text("1\n", encoding="utf-8")
        (artifact_root / "ans" / "001.ans").write_text("42\n", encoding="utf-8")
        (artifact_root / "logs" / "run_config.json").write_text(
            json.dumps(
                {
                    "checker_mode": "testlib",
                    "checker_args": [],
                    "max_passes": 4,
                    "run_jobs": 1,
                    "mode": "multi-pass",
                    "time_limit_ms": 1000,
                    "run_memory_mb": 512,
                    "run_process_limit": 64,
                    "run_output_kb": 16384,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        checker_src = Path("third_party/upstream/testlib/checkers/ncmp.cpp")
        checker_bin = artifact_root / "bin" / "checker"
        ok, cout, cerr, _ = toolchain_service.compile_cpp(
            checker_src,
            checker_bin,
            include_dirs=[Path("third_party/upstream/testlib")],
            path_roots=[Path("."), Path("third_party/upstream/testlib")],
        )
        self.assertTrue(ok, msg=f"standard checker compile failed\nstdout:\n{cout}\nstderr:\n{cerr}")

        interactor_src = artifact_root / "logs" / "mp_interactor.cpp"
        interactor_src.write_text(
            """#include "testlib.h"
int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);
    int phase = inf.readInt();
    if (phase == 1) {
        std::printf("P1\\n");
        std::fflush(stdout);
        std::string token = ouf.readToken();
        if (token != "hello") quitf(_wa, "expected hello");
        tout << 2 << "\\n";
        quitf(_ok, "pass1");
    }
    if (phase == 2) {
        std::printf("P2\\n");
        std::fflush(stdout);
        std::string token = ouf.readToken();
        if (token != "42") quitf(_wa, "expected 42");
        tout << 42 << "\\n";
        quitf(_ok, "pass2");
    }
    quitf(_fail, "unexpected phase");
}
""",
            encoding="utf-8",
        )
        interactor_bin = artifact_root / "bin" / "interactor"
        ok, cout, cerr, _ = toolchain_service.compile_cpp(
            interactor_src,
            interactor_bin,
            include_dirs=[Path("third_party/upstream/testlib")],
            path_roots=[Path("."), Path("third_party/upstream/testlib"), artifact_root],
        )
        self.assertTrue(ok, msg=f"interactor compile failed\nstdout:\n{cout}\nstderr:\n{cerr}")

        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
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

        run_id = run_service.run_submission(
            self.problem,
            self.user,
            build_id,
            submission_path="solutions/ac.cpp",
            mode="multi-pass",
        )
        row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "ok")
        summary = json.loads(str(row["summary_json"] or "{}"))
        tests = summary.get("tests")
        self.assertIsInstance(tests, list)
        self.assertTrue(tests)
        first = tests[0] if isinstance(tests[0], dict) else {}
        passes = first.get("passes")
        self.assertIsInstance(passes, list)
        self.assertEqual(len(passes), 1)
        final_pass = passes[0] if isinstance(passes[0], dict) else {}
        self.assertEqual(str(final_pass.get("verdict") or ""), str(first.get("verdict") or ""))

