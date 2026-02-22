from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.toolchain_service import ToolchainService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")


class RunService:
    def __init__(self, db: DB, workspace_service: WorkspaceService, toolchain: ToolchainService):
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain

    def _collect_diagnostics(self, workspace: Path | None, text: str) -> list[dict]:
        result: list[dict] = []
        for line in text.splitlines():
            m = DIAG_RE.match(line.strip())
            if not m:
                continue
            file_path = Path(m.group("file"))
            rel = str(file_path)
            can_link = False
            if workspace is not None:
                try:
                    if file_path.is_absolute():
                        rel = str(file_path.resolve().relative_to(workspace.resolve()))
                    else:
                        abs_path = (workspace / file_path).resolve()
                        rel = str(abs_path.relative_to(workspace.resolve()))
                    can_link = True
                except Exception:
                    rel = str(file_path)
                    can_link = False
            result.append(
                {
                    "file": rel,
                    "line": int(m.group("line")),
                    "column": int(m.group("col")),
                    "level": m.group("level"),
                    "message": m.group("msg"),
                    "can_link": can_link,
                }
            )
        return result

    def _resolve_submission_source(self, workspace: Path, submission_path: str) -> Path:
        source = (workspace / submission_path).resolve()
        ws_resolved = workspace.resolve()
        if ws_resolved not in source.parents:
            raise RuntimeError("submission_path must be inside the workspace")
        if not source.exists() or not source.is_file():
            raise RuntimeError(f"submission source not found: {submission_path}")
        return source

    def _validator_style_verdict(self, rc: int) -> str:
        if rc in {0, 42}:
            return "OK"
        if rc == 43:
            return "WA"
        return "FAIL"

    def _load_run_config(self, artifact_root: Path) -> dict[str, object]:
        cfg: dict[str, object] = {
            "checker_mode": "testlib",
            "checker_args": [],
            "max_passes": 16,
        }
        manifest = artifact_root / "manifest.json"
        if manifest.exists():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                params = payload.get("generation_params")
                if isinstance(params, dict):
                    cfg.update(params)
            except Exception:
                pass
        checker_mode = str(cfg.get("checker_mode", "testlib")).lower()
        if checker_mode not in {"testlib", "kattis"}:
            checker_mode = "testlib"
        checker_args = cfg.get("checker_args", [])
        if not isinstance(checker_args, list):
            checker_args = []
        try:
            max_passes = max(1, int(cfg.get("max_passes", 16)))
        except Exception:
            max_passes = 16
        return {
            "checker_mode": checker_mode,
            "checker_args": [str(x) for x in checker_args],
            "max_passes": max_passes,
        }

    def _run_interactive_case(
        self,
        interactor_bin: Path,
        submission_bin: Path,
        test: Path,
        ans: Path,
        transcript: Path,
        feedback_dir: Path,
        timeout_sec: int = 30,
    ) -> tuple[str, int, int]:
        sub = subprocess.Popen(
            [str(submission_bin)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        itr_env = dict(os.environ)
        itr_env["FEEDBACK_DIR"] = str(feedback_dir)
        itr = subprocess.Popen(
            [str(interactor_bin), str(test), str(ans)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            env=itr_env,
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
                        return "TLE", int((time.monotonic() - start) * 1000), 0
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
                return "RE", int((time.monotonic() - start) * 1000), 0
            itr_verdict = self._validator_style_verdict(itr.returncode or 0)
            if itr_verdict != "OK":
                err = itr.stderr.read() if itr.stderr else b""
                tf.write(f"interactor stderr:\n{err.decode('utf-8', errors='replace')}\n")
                return itr_verdict, int((time.monotonic() - start) * 1000), 0
            return "OK", int((time.monotonic() - start) * 1000), 0

    def _run_pass(
        self,
        submission_bin: Path,
        input_file: Path,
        output_file: Path,
        timeout_sec: int,
        time_file: Path,
    ) -> tuple[int, int, int]:
        if time_file.exists():
            time_file.unlink()
        if Path("/usr/bin/time").exists():
            cmd = ["/usr/bin/time", "-f", "%M", "-o", str(time_file), str(submission_bin)]
        else:
            cmd = [str(submission_bin)]

        proc = run_cmd(cmd, stdin_path=input_file, stdout_path=output_file, timeout=timeout_sec)
        mem_kb = 0
        if time_file.exists():
            raw = time_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                mem_kb = int(raw)
        return proc.returncode, proc.elapsed_ms, mem_kb

    def _feedback_key_files(self, test_feedback_dir: Path, artifact_root: Path) -> list[str]:
        keys = []
        for name in ["judgemessage.txt", "teammessage.txt", "nextpass.in"]:
            for p in sorted(test_feedback_dir.rglob(name)):
                keys.append(str(p.relative_to(artifact_root)))
        return keys

    def _files_equal(self, lhs: Path, rhs: Path) -> bool:
        if not lhs.exists() or not rhs.exists():
            return False
        if lhs.stat().st_size != rhs.stat().st_size:
            return False
        chunk = 1024 * 1024
        with lhs.open("rb") as fa, rhs.open("rb") as fb:
            while True:
                a = fa.read(chunk)
                b = fb.read(chunk)
                if a != b:
                    return False
                if not a:
                    return True

    def run_submission(
        self,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
    ) -> str:
        if mode not in {"pass-fail", "interactive", "multi-pass"}:
            raise ValueError(f"unsupported run mode: {mode}")

        run_id = f"r-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        artifact_root = Path(self.workspace_service.settings.artifacts_root) / problem / build_id
        build_artifact_exists = artifact_root.exists()
        build_row = self.db.fetch_one("SELECT problem_id,status FROM builds WHERE id=?", [build_id])
        run_root = artifact_root / "logs" / f"run-{run_id}"
        run_root.mkdir(parents=True, exist_ok=True)
        compile_log_file = run_root / "compile.log"

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

        if (
            build_row is None
            or build_row["problem_id"] != ctx["problem"]["id"]
            or build_row["status"] != "ok"
            or not build_artifact_exists
        ):
            error = f"build not runnable: {build_id}"
            compile_log_file.write_text(error + "\n", encoding="utf-8")
            summary = {
                "error": error,
                "mode": mode,
                "source": submission_path or "upload",
                "tests": [],
                "compile_log": "compile.log",
                "compile_diagnostics": [],
                "toolchain_digest": "unknown",
            }
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["failed", json.dumps(summary), now_iso(), run_id],
            )
            return run_id

        run_cfg = self._load_run_config(artifact_root)
        checker_mode = str(run_cfg["checker_mode"])
        checker_args = [str(x) for x in run_cfg["checker_args"]]
        max_passes = int(run_cfg["max_passes"])

        tests_dir = artifact_root / "tests"
        ans_dir = artifact_root / "ans"
        checker = artifact_root / "bin" / "checker"
        interactor = artifact_root / "bin" / "interactor"
        feedback_dir = run_root / "feedback_dir"
        feedback_dir.mkdir(parents=True, exist_ok=True)

        workspace = Path(ctx["workspace"]["path"])
        sub_bin = run_root / "submission"
        compile_diagnostics: list[dict] = []
        source_label = submission_path or "upload"
        verdicts = []
        toolchain_digest = "unknown"
        compile_workspace: Path | None = None
        try:
            if upload_content:
                suffix = Path(upload_filename or "submission.cpp").suffix or ".cpp"
                sub_src = run_root / f"uploaded_submission{suffix}"
                sub_src.write_bytes(upload_content)
                source_label = upload_filename or sub_src.name
                compile_workspace = None
            else:
                if not submission_path:
                    raise RuntimeError("submission_path is required when upload is not provided")
                sub_src = self._resolve_submission_source(workspace, submission_path)
                source_label = submission_path
                compile_workspace = workspace

            include_dirs: list[Path] = []
            include_dir = workspace / "third_party" / "testlib"
            if include_dir.exists():
                include_dirs.append(include_dir)

            if compile_workspace is not None:
                with self.workspace_service.workspace_lock(workspace):
                    ok, cout, cerr, toolchain_digest = self.toolchain.compile_cpp(
                        sub_src,
                        sub_bin,
                        include_dirs,
                        path_roots=[compile_workspace],
                    )
            else:
                ok, cout, cerr, toolchain_digest = self.toolchain.compile_cpp(
                    sub_src,
                    sub_bin,
                    include_dirs,
                    path_roots=[run_root, workspace],
                )

            compile_log = f"{cout}\n{cerr}".strip()
            compile_log_file.write_text((compile_log + "\n") if compile_log else "", encoding="utf-8")
            compile_diagnostics = self._collect_diagnostics(compile_workspace, compile_log)
            if not ok:
                summary = {
                    "error": "compile_error",
                    "compile_log": "compile.log",
                    "compile_diagnostics": compile_diagnostics,
                    "toolchain_digest": toolchain_digest,
                    "source": source_label,
                    "run_config": run_cfg,
                }
                (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
                self.db.execute(
                    "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                    ["failed", json.dumps(summary), now_iso(), run_id],
                )
                return run_id

            tests = sorted(tests_dir.glob("*.in"))
            if not tests:
                raise RuntimeError("selected build has no tests")

            for test in tests:
                ans = ans_dir / f"{test.stem}.ans"
                test_feedback_dir = feedback_dir / test.stem
                test_feedback_dir.mkdir(parents=True, exist_ok=True)
                test_result = {
                    "test": test.name,
                    "passes": [],
                    "verdict": "OK",
                    "time_ms": 0,
                    "memory_kb": 0,
                    "feedback_files": [],
                }

                if mode == "interactive":
                    if not interactor.exists():
                        raise RuntimeError("interactive mode requested but interactor is missing in build artifacts")
                    transcript = run_root / f"{test.stem}.transcript.txt"
                    verdict, elapsed, mem_kb = self._run_interactive_case(
                        interactor,
                        sub_bin,
                        test,
                        ans,
                        transcript,
                        test_feedback_dir,
                    )
                    test_result["passes"].append({"pass": 1, "verdict": verdict, "time_ms": elapsed, "memory_kb": mem_kb})
                    test_result["verdict"] = verdict
                    test_result["time_ms"] = elapsed
                    test_result["memory_kb"] = mem_kb
                    test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, artifact_root)
                    test_result["transcript"] = str(transcript.relative_to(artifact_root))
                    verdicts.append(test_result)
                    continue

                current_input = test
                pass_idx = 1
                total_time = 0
                peak_mem = 0
                while True:
                    pass_feedback_dir = test_feedback_dir / f"pass{pass_idx}"
                    pass_feedback_dir.mkdir(parents=True, exist_ok=True)
                    out = run_root / f"{test.stem}.pass{pass_idx}.out"
                    time_file = pass_feedback_dir / "time.txt"
                    exec_rc, exec_ms, exec_mem = self._run_pass(sub_bin, current_input, out, 30, time_file)
                    total_time += exec_ms
                    peak_mem = max(peak_mem, exec_mem)
                    if exec_rc != 0:
                        p = {"pass": pass_idx, "verdict": "RE", "time_ms": exec_ms, "memory_kb": exec_mem}
                        test_result["passes"].append(p)
                        test_result["verdict"] = "RE"
                        break

                    checker_verdict = "OK"
                    if checker.exists():
                        env = dict(os.environ)
                        env["FEEDBACK_DIR"] = str(pass_feedback_dir)
                        if checker_mode == "kattis":
                            feedback_arg = str(pass_feedback_dir) + os.sep
                            check_proc = run_cmd(
                                [str(checker), str(test), str(ans), feedback_arg, *checker_args],
                                stdin_path=out,
                                timeout=30,
                                cwd=pass_feedback_dir,
                                env=env,
                            )
                        else:
                            check_proc = run_cmd(
                                [str(checker), str(test), str(out), str(ans), *checker_args],
                                timeout=30,
                                cwd=pass_feedback_dir,
                                env=env,
                        )
                        (pass_feedback_dir / "checker.log").write_text(
                            check_proc.stdout + check_proc.stderr,
                            encoding="utf-8",
                        )
                        checker_verdict = self._validator_style_verdict(check_proc.returncode)
                    else:
                        checker_verdict = "OK" if self._files_equal(ans, out) else "WA"

                    p = {"pass": pass_idx, "verdict": checker_verdict, "time_ms": exec_ms, "memory_kb": exec_mem}
                    test_result["passes"].append(p)

                    if mode != "multi-pass":
                        test_result["verdict"] = checker_verdict
                        break

                    next_pass = pass_feedback_dir / "nextpass.in"
                    if checker_verdict != "OK" or not next_pass.exists() or pass_idx >= max_passes:
                        test_result["verdict"] = checker_verdict
                        break

                    current_input = next_pass
                    pass_idx += 1

                test_result["time_ms"] = total_time
                test_result["memory_kb"] = peak_mem
                test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, artifact_root)
                verdicts.append(test_result)

            summary = {
                "mode": mode,
                "source": source_label,
                "tests": verdicts,
                "feedback_dir": str(feedback_dir),
                "compile_diagnostics": compile_diagnostics,
                "compile_log": "compile.log",
                "toolchain_digest": toolchain_digest,
                "run_config": run_cfg,
            }
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["ok", json.dumps(summary), now_iso(), run_id],
            )
        except Exception as exc:
            if not compile_log_file.exists():
                compile_log_file.write_text(str(exc) + "\n", encoding="utf-8")
            summary = {
                "error": str(exc),
                "mode": mode,
                "source": source_label,
                "tests": verdicts,
                "feedback_dir": str(feedback_dir),
                "compile_diagnostics": compile_diagnostics,
                "compile_log": "compile.log",
                "toolchain_digest": toolchain_digest,
                "run_config": run_cfg,
            }
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self.db.execute(
                "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["failed", json.dumps(summary), now_iso(), run_id],
            )

        return run_id
