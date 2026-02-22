from __future__ import annotations

import json
import os
import selectors
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


class RunService:
    def __init__(self, db: DB, workspace_service: WorkspaceService):
        self.db = db
        self.workspace_service = workspace_service

    def _run_interactive_case(
        self,
        interactor_bin: Path,
        submission_bin: Path,
        test: Path,
        ans: Path,
        transcript: Path,
        timeout_sec: int = 30,
    ) -> tuple[str, int]:
        sub = subprocess.Popen(
            [str(submission_bin)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        itr = subprocess.Popen(
            [str(interactor_bin), str(test), str(ans)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        start = time.monotonic()
        sel = selectors.DefaultSelector()
        if itr.stdout:
            sel.register(itr.stdout, selectors.EVENT_READ, ("itr", "out"))
        if sub.stdout:
            sel.register(sub.stdout, selectors.EVENT_READ, ("sub", "out"))
        with transcript.open("w", encoding="utf-8") as tf:
            try:
                while True:
                    if time.monotonic() - start > timeout_sec:
                        sub.kill()
                        itr.kill()
                        return "TLE", int((time.monotonic() - start) * 1000)
                    events = sel.select(timeout=0.2)
                    for key, _ in events:
                        stream_owner, _stream_kind = key.data
                        data = os.read(key.fileobj.fileno(), 4096)
                        if not data:
                            sel.unregister(key.fileobj)
                            continue
                        decoded = data.decode("utf-8", errors="replace")
                        if stream_owner == "itr":
                            tf.write(f"I> {decoded}")
                            if sub.stdin:
                                sub.stdin.write(data)
                                sub.stdin.flush()
                        else:
                            tf.write(f"S> {decoded}")
                            if itr.stdin:
                                itr.stdin.write(data)
                                itr.stdin.flush()
                    tf.flush()
                    if sub.poll() is not None and itr.poll() is not None:
                        break
            finally:
                sel.close()
                if sub.stdin:
                    sub.stdin.close()
                if itr.stdin:
                    itr.stdin.close()

            sub.wait(timeout=2)
            itr.wait(timeout=2)
            if sub.returncode != 0:
                err = sub.stderr.read() if sub.stderr else b""
                tf.write(f"submission stderr:\n{err.decode('utf-8', errors='replace')}\n")
                return "RE", int((time.monotonic() - start) * 1000)
            if itr.returncode != 0:
                err = itr.stderr.read() if itr.stderr else b""
                tf.write(f"interactor stderr:\n{err.decode('utf-8', errors='replace')}\n")
                return "WA", int((time.monotonic() - start) * 1000)
            return "OK", int((time.monotonic() - start) * 1000)

    def run_submission(
        self,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str,
        mode: str = "pass-fail",
    ) -> str:
        run_id = f"r-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        artifact_root = Path(self.workspace_service.settings.artifacts_root) / problem / build_id
        run_root = artifact_root / "logs" / f"run-{run_id}"
        run_root.mkdir(parents=True, exist_ok=True)

        self.db.execute(
            "INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [
                run_id,
                ctx["problem"]["id"],
                ctx["workspace"]["id"],
                build_id,
                mode,
                "running",
                str(run_root),
                now_iso(),
            ],
        )

        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        checker = artifact_root / "bin" / "checker"
        interactor = artifact_root / "bin" / "interactor"
        feedback_dir = run_root / "feedback_dir"
        feedback_dir.mkdir(parents=True, exist_ok=True)

        workspace = Path(ctx["workspace"]["path"])
        sub_src = workspace / submission_path
        sub_bin = run_root / "submission"

        with self.workspace_service.workspace_lock(workspace):
            cproc = run_cmd(["g++", "-O2", "-std=c++20", str(sub_src), "-o", str(sub_bin)])
        if cproc.returncode != 0:
            (run_root / "compile.log").write_text(cproc.stdout + cproc.stderr, encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["failed", json.dumps({"error": "compile_error", "compile_log": "compile.log"}), now_iso(), run_id],
            )
            return run_id

        verdicts = []
        try:
            for test in sorted(tests_dir.glob("*.in")):
                ans = ans_dir / f"{test.stem}.ans"
                test_result = {"test": test.name, "passes": [], "verdict": "OK", "time_ms": 0, "memory_kb": 0}

                if mode == "interactive":
                    if not interactor.exists():
                        raise RuntimeError("interactive mode requested but interactor is missing in build artifacts")
                    transcript = run_root / f"{test.stem}.transcript.txt"
                    verdict, elapsed = self._run_interactive_case(interactor, sub_bin, test, ans, transcript)
                    test_result["passes"].append({"pass": 1, "verdict": verdict, "time_ms": elapsed, "memory_kb": 0})
                    test_result["verdict"] = verdict
                    test_result["time_ms"] = elapsed
                    verdicts.append(test_result)
                    continue

                current_input = test
                pass_idx = 1
                total_time = 0
                while True:
                    out = run_root / f"{test.stem}.pass{pass_idx}.out"
                    exec_cmd = f"{shlex.quote(str(sub_bin))} < {shlex.quote(str(current_input))} > {shlex.quote(str(out))}"
                    exec_proc = run_cmd(["bash", "-lc", exec_cmd], timeout=30)
                    total_time += exec_proc.elapsed_ms
                    if exec_proc.returncode != 0:
                        p = {"pass": pass_idx, "verdict": "RE", "time_ms": exec_proc.elapsed_ms, "memory_kb": 0}
                        test_result["passes"].append(p)
                        test_result["verdict"] = "RE"
                        break

                    checker_verdict = "OK"
                    if checker.exists():
                        env = dict(os.environ)
                        env["FEEDBACK_DIR"] = str(feedback_dir)
                        check_proc = run_cmd(
                            [str(checker), str(test), str(out), str(ans)],
                            timeout=30,
                            cwd=feedback_dir,
                            env=env,
                        )
                        checker_verdict = "OK" if check_proc.returncode == 0 else "WA"
                    else:
                        checker_verdict = "OK" if ans.exists() and ans.read_text(encoding="utf-8") == out.read_text(encoding="utf-8") else "WA"

                    p = {"pass": pass_idx, "verdict": checker_verdict, "time_ms": exec_proc.elapsed_ms, "memory_kb": 0}
                    test_result["passes"].append(p)

                    if mode != "multi-pass":
                        test_result["verdict"] = checker_verdict
                        break

                    next_pass = feedback_dir / "nextpass.in"
                    if checker_verdict != "OK" or not next_pass.exists() or pass_idx >= 16:
                        test_result["verdict"] = checker_verdict
                        break

                    current_input = next_pass
                    pass_idx += 1

                test_result["time_ms"] = total_time
                verdicts.append(test_result)

            summary = {"mode": mode, "tests": verdicts, "feedback_dir": str(feedback_dir)}
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["ok", json.dumps(summary), now_iso(), run_id],
            )
        except Exception as exc:
            summary = {"error": str(exc), "mode": mode, "tests": verdicts, "feedback_dir": str(feedback_dir)}
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["failed", json.dumps(summary), now_iso(), run_id],
            )

        return run_id
