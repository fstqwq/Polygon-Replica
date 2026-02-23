from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO

from app.db import DB, now_iso
from app.services.toolchain_service import ToolchainService
from app.services.util import is_canonical_artifact_id, run_cmd
from app.services.workspace_service import WorkspaceService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")


class RunService:
    RUN_TIMEOUT_SENTINEL = -1_000_000_000
    ARTIFACT_CACHE_LIMIT = 256
    FEEDBACK_KEY_FILE_LIMIT = 256

    def __init__(self, db: DB, workspace_service: WorkspaceService, toolchain: ToolchainService):
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain
        self._run_config_cache: dict[str, dict[str, object]] = {}
        self._test_input_cache: dict[str, list[str]] = {}
        self._answer_file_cache: dict[str, list[str]] = {}
        self._test_input_meta_cache: dict[str, list[tuple[str, str]]] = {}
        self._cache_lock = threading.Lock()

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
        candidate = workspace / submission_path
        ws_resolved = workspace.resolve()
        source = candidate.resolve()
        if ws_resolved not in source.parents:
            raise RuntimeError("submission_path must be inside the workspace")
        if self._contains_symlink_component(ws_resolved, candidate):
            raise RuntimeError("submission_path cannot include symlink path components")
        try:
            rel_parts = source.relative_to(ws_resolved).parts
        except ValueError:
            raise RuntimeError("submission_path must be inside the workspace")
        if ".git" in rel_parts or ".polygonlike.lock" in rel_parts:
            raise RuntimeError("submission_path is reserved")
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
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._run_config_cache, cache_key)
        if cached is not None:
            return dict(cached)

        cfg: dict[str, object] = {
            "checker_mode": "testlib",
            "checker_args": [],
            "max_passes": 16,
            "run_jobs": 0,
            "run_timeout_sec": 30,
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
        try:
            run_jobs = max(0, min(16, int(cfg.get("run_jobs", 0))))
        except Exception:
            run_jobs = 0
        try:
            run_timeout_sec = max(1, min(300, int(cfg.get("run_timeout_sec", 30))))
        except Exception:
            run_timeout_sec = 30
        resolved_cfg = {
            "checker_mode": checker_mode,
            "checker_args": [str(x) for x in checker_args],
            "max_passes": max_passes,
            "run_jobs": run_jobs,
            "run_timeout_sec": run_timeout_sec,
        }
        self._cache_put(self._run_config_cache, cache_key, dict(resolved_cfg))
        return dict(resolved_cfg)

    def _load_test_inputs(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_cache, cache_key)
        if cached is not None:
            return list(cached)
        tests_dir = artifact_root / "tests"
        names = [p.name for p in self._safe_matching_files(tests_dir, "*.in")]
        self._cache_put(self._test_input_cache, cache_key, list(names))
        return list(names)

    def _load_answer_files(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._answer_file_cache, cache_key)
        if cached is not None:
            return list(cached)
        ans_dir = artifact_root / "ans"
        names = [p.name for p in self._safe_matching_files(ans_dir, "*.ans")]
        self._cache_put(self._answer_file_cache, cache_key, list(names))
        return list(names)

    def _load_test_input_meta(self, artifact_root: Path) -> list[tuple[str, str]]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_meta_cache, cache_key)
        if cached is not None:
            return list(cached)
        meta = [(name, Path(name).stem) for name in self._load_test_inputs(artifact_root)]
        self._cache_put(self._test_input_meta_cache, cache_key, list(meta))
        return list(meta)

    def _artifact_cache_key(self, artifact_root: Path) -> str:
        try:
            return str(artifact_root.resolve())
        except OSError:
            return str(artifact_root)

    def _contains_symlink_component(self, root: Path, candidate: Path) -> bool:
        try:
            if root.is_symlink():
                return True
        except OSError:
            return True
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            return True
        cur = root
        for part in rel.parts:
            cur = cur / part
            try:
                if cur.is_symlink():
                    return True
            except OSError:
                return True
            if not cur.exists():
                break
        return False

    def _cache_get(self, cache: dict, key: str):
        with self._cache_lock:
            value = cache.get(key)
            if value is None:
                return None
            # Promote on access to keep hot build artifacts resident.
            cache.pop(key, None)
            cache[key] = value
            return value

    def _cache_put(self, cache: dict, key: str, value) -> None:
        with self._cache_lock:
            cache.pop(key, None)
            cache[key] = value
            while len(cache) > self.ARTIFACT_CACHE_LIMIT:
                oldest_key = next(iter(cache))
                cache.pop(oldest_key, None)

    def _effective_run_jobs(self, configured: object, test_count: int) -> int:
        auto_jobs = max(1, min(4, os.cpu_count() or 1))
        try:
            requested = int(configured)
        except Exception:
            requested = 0
        bounded = auto_jobs if requested <= 0 else max(1, min(16, requested))
        return max(1, min(bounded, max(1, test_count)))

    def _canonical_build_artifact_root(self, problem: str, build_id: str) -> Path:
        aid = str(build_id or "")
        if not is_canonical_artifact_id(aid):
            raise RuntimeError("invalid build artifact id")
        base = (Path(self.workspace_service.settings.artifacts_root) / problem).resolve()
        root = (base / aid).resolve()
        try:
            rel = root.relative_to(base)
        except ValueError as exc:
            raise RuntimeError("invalid build artifact id") from exc
        if len(rel.parts) != 1 or rel.parts[0] != aid:
            raise RuntimeError("invalid build artifact id")
        return root

    def _is_safe_path_within(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _is_safe_dir(self, root: Path, path: Path) -> bool:
        if path.is_symlink() or not path.exists() or not path.is_dir():
            return False
        return self._is_safe_path_within(root, path)

    def _is_safe_regular_file(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return False
        return self._is_safe_path_within(root, path, root_resolved=root_resolved)

    def _safe_matching_files(self, root: Path, pattern: str) -> list[Path]:
        try:
            root_resolved = root.resolve()
        except OSError:
            return []
        files: list[Path] = []
        # Fast path for common suffix-only patterns used by run discovery (for example *.in / *.ans).
        if (
            pattern.startswith("*.")
            and pattern.count("*") == 1
            and "?" not in pattern
            and "[" not in pattern
            and "]" not in pattern
        ):
            suffix = pattern[1:]
            try:
                candidates = sorted(root.iterdir(), key=lambda p: p.name)
            except OSError:
                return []
            for p in candidates:
                if not p.name.endswith(suffix):
                    continue
                if self._is_safe_regular_file(root, p, root_resolved=root_resolved):
                    files.append(p)
            return files

        for p in sorted(root.glob(pattern)):
            if self._is_safe_regular_file(root, p, root_resolved=root_resolved):
                files.append(p)
        return files

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
        sub_err_path = transcript.with_name(f"{transcript.stem}.submission.stderr.txt")
        itr_err_path = transcript.with_name(f"{transcript.stem}.interactor.stderr.txt")
        with sub_err_path.open("wb") as sub_err_fh, itr_err_path.open("wb") as itr_err_fh:
            sub = subprocess.Popen(
                [str(submission_bin)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sub_err_fh,
                text=False,
                bufsize=0,
            )
            itr_env = dict(os.environ)
            itr_env["FEEDBACK_DIR"] = str(feedback_dir)
            itr = subprocess.Popen(
                [str(interactor_bin), str(test), str(ans)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=itr_err_fh,
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
            timed_out = False
            with transcript.open("w", encoding="utf-8") as tf:
                try:
                    while True:
                        if time.monotonic() - start > timeout_sec:
                            timed_out = True
                            sub.kill()
                            itr.kill()
                            break
                        events = sel.select(timeout=0.2)
                        for key, _ in events:
                            stream_owner, _stream_kind = key.data
                            try:
                                data = os.read(key.fileobj.fileno(), 4096)
                            except OSError:
                                data = b""
                            if not data:
                                try:
                                    sel.unregister(key.fileobj)
                                except Exception:
                                    pass
                                if stream_owner == "itr" and sub.stdin:
                                    try:
                                        sub.stdin.close()
                                    except Exception:
                                        pass
                                    sub.stdin = None
                                if stream_owner == "sub" and itr.stdin:
                                    try:
                                        itr.stdin.close()
                                    except Exception:
                                        pass
                                    itr.stdin = None
                                continue
                            decoded = data.decode("utf-8", errors="replace")
                            if stream_owner == "itr":
                                tf.write(f"I> {decoded}")
                                if sub.stdin:
                                    try:
                                        sub.stdin.write(data)
                                        sub.stdin.flush()
                                    except (BrokenPipeError, OSError, ValueError):
                                        try:
                                            sub.stdin.close()
                                        except Exception:
                                            pass
                                        sub.stdin = None
                            else:
                                tf.write(f"S> {decoded}")
                                if itr.stdin:
                                    try:
                                        itr.stdin.write(data)
                                        itr.stdin.flush()
                                    except (BrokenPipeError, OSError, ValueError):
                                        try:
                                            itr.stdin.close()
                                        except Exception:
                                            pass
                                        itr.stdin = None
                        tf.flush()
                        if sub.poll() is not None and itr.poll() is not None:
                            break
                finally:
                    sel.close()
                    if sub.stdin:
                        sub.stdin.close()
                    if itr.stdin:
                        itr.stdin.close()

                for proc in [sub, itr]:
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass

                sub_err_fh.flush()
                itr_err_fh.flush()
                elapsed = int((time.monotonic() - start) * 1000)
                if timed_out:
                    return "TLE", elapsed, 0

                if sub.returncode != 0:
                    err = sub_err_path.read_text(encoding="utf-8", errors="replace") if sub_err_path.exists() else ""
                    tf.write(f"submission stderr:\n{err}\n")
                    return "RE", elapsed, 0
                itr_verdict = self._validator_style_verdict(itr.returncode or 0)
                if itr_verdict != "OK":
                    err = itr_err_path.read_text(encoding="utf-8", errors="replace") if itr_err_path.exists() else ""
                    tf.write(f"interactor stderr:\n{err}\n")
                    return itr_verdict, elapsed, 0
                return "OK", elapsed, 0

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

        elapsed_ms = timeout_sec * 1000
        returncode = self.RUN_TIMEOUT_SENTINEL
        try:
            proc = run_cmd(cmd, stdin_path=input_file, stdout_path=output_file, timeout=timeout_sec)
            elapsed_ms = proc.elapsed_ms
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            pass
        mem_kb = 0
        if time_file.exists():
            raw = time_file.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                mem_kb = int(raw)
        return returncode, elapsed_ms, mem_kb

    def _feedback_key_files(self, test_feedback_dir: Path, base_root: Path) -> list[str]:
        if not test_feedback_dir.exists() or not test_feedback_dir.is_dir():
            return []
        try:
            base_root_resolved = base_root.resolve()
        except OSError:
            return []
        if test_feedback_dir.is_symlink():
            return []
        try:
            test_feedback_resolved = test_feedback_dir.resolve()
        except OSError:
            return []
        if base_root_resolved not in test_feedback_resolved.parents and base_root_resolved != test_feedback_resolved:
            return []
        wanted = {"judgemessage.txt", "teammessage.txt", "nextpass.in"}
        cap = max(1, int(self.FEEDBACK_KEY_FILE_LIMIT))
        keys: list[str] = []
        stop_scan = False
        for dirpath, dirnames, filenames in os.walk(test_feedback_dir, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            keep_dirs: list[str] = []
            for name in sorted(dirnames):
                d = dir_root / name
                if d.is_symlink():
                    continue
                try:
                    resolved = d.resolve()
                except OSError:
                    continue
                if base_root_resolved in resolved.parents or base_root_resolved == resolved:
                    keep_dirs.append(name)
            dirnames[:] = keep_dirs

            for name in sorted(filenames):
                if name not in wanted:
                    continue
                p = dir_root / name
                if not self._is_safe_regular_file(base_root, p, root_resolved=base_root_resolved):
                    continue
                keys.append(str(p.relative_to(base_root)))
                if len(keys) >= cap:
                    stop_scan = True
                    break
            if stop_scan:
                break
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

    def _run_noninteractive_test(
        self,
        mode: str,
        sub_bin: Path,
        checker: Path,
        checker_mode: str,
        checker_args: list[str],
        max_passes: int,
        run_timeout_sec: int,
        test: Path,
        ans: Path,
        test_feedback_dir: Path,
        run_root: Path,
    ) -> dict:
        test_feedback_dir.mkdir(parents=True, exist_ok=True)
        test_result = {
            "test": test.name,
            "passes": [],
            "verdict": "OK",
            "time_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        current_input = test
        pass_idx = 1
        total_time = 0
        peak_mem = 0
        while True:
            pass_feedback_dir = test_feedback_dir / f"pass{pass_idx}"
            pass_feedback_dir.mkdir(parents=True, exist_ok=True)
            out = run_root / f"{test.stem}.pass{pass_idx}.out"
            time_file = pass_feedback_dir / "time.txt"
            exec_rc, exec_ms, exec_mem = self._run_pass(sub_bin, current_input, out, run_timeout_sec, time_file)
            total_time += exec_ms
            peak_mem = max(peak_mem, exec_mem)
            if exec_rc == self.RUN_TIMEOUT_SENTINEL:
                p = {"pass": pass_idx, "verdict": "TLE", "time_ms": exec_ms, "memory_kb": exec_mem}
                test_result["passes"].append(p)
                test_result["verdict"] = "TLE"
                break
            if exec_rc != 0:
                p = {"pass": pass_idx, "verdict": "RE", "time_ms": exec_ms, "memory_kb": exec_mem}
                test_result["passes"].append(p)
                test_result["verdict"] = "RE"
                break

            checker_verdict = "OK"
            if checker.exists():
                env = dict(os.environ)
                env["FEEDBACK_DIR"] = str(pass_feedback_dir)
                checker_log = ""
                checker_timed_out = False
                if checker_mode == "kattis":
                    feedback_arg = str(pass_feedback_dir) + os.sep
                    try:
                        check_proc = run_cmd(
                            [str(checker), str(test), str(ans), feedback_arg, *checker_args],
                            stdin_path=out,
                            timeout=run_timeout_sec,
                            cwd=pass_feedback_dir,
                            env=env,
                        )
                        checker_log = check_proc.stdout + check_proc.stderr
                    except subprocess.TimeoutExpired as exc:
                        checker_timed_out = True
                        checker_log = f"checker timed out after {run_timeout_sec}s: {exc}\n"
                else:
                    try:
                        check_proc = run_cmd(
                            [str(checker), str(test), str(out), str(ans), *checker_args],
                            timeout=run_timeout_sec,
                            cwd=pass_feedback_dir,
                            env=env,
                        )
                        checker_log = check_proc.stdout + check_proc.stderr
                    except subprocess.TimeoutExpired as exc:
                        checker_timed_out = True
                        checker_log = f"checker timed out after {run_timeout_sec}s: {exc}\n"
                (pass_feedback_dir / "checker.log").write_text(checker_log, encoding="utf-8")
                if checker_timed_out:
                    checker_verdict = "FAIL"
                else:
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
        test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
        return test_result

    def run_submission(
        self,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: IO[bytes] | None = None,
    ) -> str:
        supported_modes = {"pass-fail", "interactive", "multi-pass"}

        run_id = f"r-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        artifact_root: Path | None = None
        build_row = self.db.fetch_one("SELECT problem_id,workspace_id,status FROM builds WHERE id=?", [build_id])
        preflight_reasons: list[str] = []
        if mode not in supported_modes:
            preflight_reasons.append(f"unsupported run mode: {mode}")
        try:
            artifact_root = self._canonical_build_artifact_root(problem, build_id)
        except RuntimeError as exc:
            preflight_reasons.append(str(exc))
        if build_row is None:
            preflight_reasons.append("build metadata missing")
        else:
            if build_row["problem_id"] != ctx["problem"]["id"]:
                preflight_reasons.append("build does not belong to selected problem")
            if build_row["workspace_id"] != ctx["workspace"]["id"]:
                preflight_reasons.append("build does not belong to selected workspace")
            if build_row["status"] != "ok":
                preflight_reasons.append(f"build status is {build_row['status']}")

        if artifact_root is not None:
            if not artifact_root.exists():
                preflight_reasons.append("artifact root missing")
            else:
                for required_dir in ["tests", "ans"]:
                    dir_path = artifact_root / required_dir
                    if not dir_path.exists() or not dir_path.is_dir():
                        preflight_reasons.append(f"artifact directory missing: {required_dir}/")
                    elif not self._is_safe_dir(artifact_root, dir_path):
                        preflight_reasons.append(f"artifact directory invalid: {required_dir}/")

        preflight_ok = not preflight_reasons
        preflight_error = f"build not runnable: {build_id}"
        if preflight_reasons:
            preflight_error += " (" + "; ".join(preflight_reasons) + ")"
        if preflight_ok:
            run_root = artifact_root / "logs" / f"run-{run_id}"
        else:
            run_root = Path(self.workspace_service.settings.run_root) / "invalid-runs" / run_id
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

        if not preflight_ok:
            error = preflight_error
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

        assert artifact_root is not None
        run_cfg = self._load_run_config(artifact_root)
        checker_mode = str(run_cfg["checker_mode"])
        checker_args = [str(x) for x in run_cfg["checker_args"]]
        max_passes = int(run_cfg["max_passes"])
        run_timeout_sec = int(run_cfg["run_timeout_sec"])

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
            has_uploaded_source = False
            if upload_stream is not None:
                suffix = Path(upload_filename or "submission.cpp").suffix or ".cpp"
                sub_src = run_root / f"uploaded_submission{suffix}"
                sub_src.parent.mkdir(parents=True, exist_ok=True)
                with sub_src.open("wb") as out:
                    shutil.copyfileobj(upload_stream, out, length=1024 * 1024)
                source_label = upload_filename or sub_src.name
                compile_workspace = None
                has_uploaded_source = True
            if not has_uploaded_source and upload_content is not None:
                suffix = Path(upload_filename or "submission.cpp").suffix or ".cpp"
                sub_src = run_root / f"uploaded_submission{suffix}"
                sub_src.write_bytes(upload_content)
                source_label = upload_filename or sub_src.name
                compile_workspace = None
                has_uploaded_source = True
            if not has_uploaded_source:
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

            test_meta = self._load_test_input_meta(artifact_root)
            if not test_meta:
                raise RuntimeError("selected build has no tests")
            answer_names = set(self._load_answer_files(artifact_root))
            effective_run_jobs = self._effective_run_jobs(run_cfg.get("run_jobs", 0), len(test_meta))
            run_cfg["run_jobs_effective"] = effective_run_jobs

            if checker.exists() and not self._is_safe_regular_file(checker.parent, checker):
                raise RuntimeError("checker binary path is invalid")
            if interactor.exists() and not self._is_safe_regular_file(interactor.parent, interactor):
                raise RuntimeError("interactor binary path is invalid")

            if mode == "interactive":
                for test_name, test_stem in test_meta:
                    test = tests_dir / test_name
                    ans_name = f"{test_stem}.ans"
                    if ans_name not in answer_names:
                        raise RuntimeError(f"answer file missing or invalid for {test_name}")
                    ans = ans_dir / ans_name
                    test_feedback_dir = feedback_dir / test_stem
                    test_feedback_dir.mkdir(parents=True, exist_ok=True)
                    test_result = {
                        "test": test_name,
                        "passes": [],
                        "verdict": "OK",
                        "time_ms": 0,
                        "memory_kb": 0,
                        "feedback_files": [],
                    }
                    if not interactor.exists():
                        raise RuntimeError("interactive mode requested but interactor is missing in build artifacts")
                    transcript = run_root / f"{test_stem}.transcript.txt"
                    verdict, elapsed, mem_kb = self._run_interactive_case(
                        interactor,
                        sub_bin,
                        test,
                        ans,
                        transcript,
                        test_feedback_dir,
                        timeout_sec=run_timeout_sec,
                    )
                    test_result["passes"].append({"pass": 1, "verdict": verdict, "time_ms": elapsed, "memory_kb": mem_kb})
                    test_result["verdict"] = verdict
                    test_result["time_ms"] = elapsed
                    test_result["memory_kb"] = mem_kb
                    test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
                    test_result["transcript"] = str(transcript.relative_to(run_root))
                    verdicts.append(test_result)
            elif mode != "interactive" and effective_run_jobs > 1:
                for test_name, test_stem in test_meta:
                    ans_name = f"{test_stem}.ans"
                    if ans_name not in answer_names:
                        raise RuntimeError(f"answer file missing or invalid for {test_name}")
                with ThreadPoolExecutor(max_workers=effective_run_jobs) as pool:
                    future_map = {
                        pool.submit(
                            self._run_noninteractive_test,
                            mode,
                            sub_bin,
                            checker,
                            checker_mode,
                            checker_args,
                            max_passes,
                            run_timeout_sec,
                            tests_dir / test_name,
                            ans_dir / f"{test_stem}.ans",
                            feedback_dir / test_stem,
                            run_root,
                        ): idx
                        for idx, (test_name, test_stem) in enumerate(test_meta)
                    }
                    parallel_verdicts: list[dict] = [{} for _ in test_meta]
                    for future in as_completed(future_map):
                        idx = future_map[future]
                        parallel_verdicts[idx] = future.result()
                    verdicts.extend(parallel_verdicts)
            else:
                for test_name, test_stem in test_meta:
                    test = tests_dir / test_name
                    ans_name = f"{test_stem}.ans"
                    if ans_name not in answer_names:
                        raise RuntimeError(f"answer file missing or invalid for {test_name}")
                    ans = ans_dir / ans_name
                    verdicts.append(
                        self._run_noninteractive_test(
                            mode,
                            sub_bin,
                            checker,
                            checker_mode,
                            checker_args,
                            max_passes,
                            run_timeout_sec,
                            test,
                            ans,
                            feedback_dir / test_stem,
                            run_root,
                        )
                    )

            summary = {
                "mode": mode,
                "source": source_label,
                "tests": verdicts,
                "feedback_dir": "feedback_dir",
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
                "feedback_dir": "feedback_dir",
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
