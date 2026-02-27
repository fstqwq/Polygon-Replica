from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO

from app.db import DB, now_iso
from app.services.sandbox import ExecSpec, SandboxBackend, create_sandbox_backend
from app.services.toolchain_service import ToolchainService
from app.services.util import is_canonical_artifact_id, run_cmd
from app.services.workspace_service import WorkspaceService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")
RUN_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in$")


class RunService:
    RUN_TIMEOUT_SENTINEL = -1_000_000_000
    ARTIFACT_CACHE_LIMIT = 256
    FEEDBACK_KEY_FILE_LIMIT = 256
    DB_SUMMARY_TESTS_LIMIT = 200
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 200
    DB_SUMMARY_FEEDBACK_FILES_LIMIT = 32
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096
    DEFAULT_TIME_LIMIT_MS = 2000
    TIME_LIMIT_MIN_MS = 100
    TIME_LIMIT_MAX_MS = 30000
    RUN_TIMEOUT_MAX_SEC = 300
    SUBMISSION_CPP_CXXFLAGS = ["-O2", "-std=c++20", "-pipe"]
    PATH_GUARD_SOURCE = (Path(__file__).resolve().parent / "sandbox" / "path_guard.c").resolve()

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        toolchain: ToolchainService,
        sandbox_backend: SandboxBackend | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain
        self.sandbox = sandbox_backend or create_sandbox_backend()
        self.default_run_memory_mb = self._env_int("POLYGONLIKE_RUN_MEMORY_MB", default=1024, min_value=16, max_value=262144)
        self.default_run_process_limit = self._env_int("POLYGONLIKE_RUN_PROCESS_LIMIT", default=64, min_value=1, max_value=4096)
        self.default_run_output_kb = self._env_int("POLYGONLIKE_RUN_OUTPUT_KB", default=65536, min_value=64, max_value=1048576)
        self._run_config_cache: dict[str, dict[str, object]] = {}
        self._test_input_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_set_cache: dict[str, frozenset[str]] = {}
        self._test_input_meta_cache: dict[str, tuple[tuple[str, str], ...]] = {}
        self._cache_lock = threading.Lock()
        self._path_guard_lock = threading.Lock()

    def _env_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def _normalized_path_prefixes(self, paths: list[Path] | tuple[Path, ...] | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_path in paths or []:
            text = str(raw_path or "").strip()
            if not text:
                continue
            p = Path(text)
            try:
                normalized = str(p.resolve())
            except OSError:
                normalized = str(p.absolute())
            normalized = normalized.rstrip("/") or "/"
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _ensure_path_guard_library(self) -> Path | None:
        source = self.PATH_GUARD_SOURCE
        if not source.exists() or not source.is_file():
            return None
        cache_root = Path(self.workspace_service.settings.cache_root) / "runtime"
        cache_root.mkdir(parents=True, exist_ok=True)
        target = cache_root / "path_guard.so"
        with self._path_guard_lock:
            try:
                source_mtime = source.stat().st_mtime
            except OSError:
                return None
            rebuild = True
            if target.exists():
                try:
                    rebuild = target.stat().st_mtime < source_mtime
                except OSError:
                    rebuild = True
            if rebuild:
                tmp = cache_root / f".path_guard-{uuid.uuid4().hex[:8]}.tmp.so"
                cmd = ["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-o", str(tmp), str(source), "-ldl", "-pthread"]
                proc = run_cmd(cmd, timeout=30)
                if proc.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    return None
                os.replace(tmp, target)
                try:
                    target.chmod(0o755)
                except OSError:
                    pass
            if not target.exists() or not target.is_file():
                return None
            return target

    def _path_guard_environment(
        self,
        *,
        base_env: dict[str, str] | None,
        deny_paths: list[Path] | tuple[Path, ...] | None,
        allow_paths: list[Path] | tuple[Path, ...] | None,
    ) -> dict[str, str] | None:
        deny_prefixes = self._normalized_path_prefixes(deny_paths)
        if not deny_prefixes:
            return base_env
        path_guard_so = self._ensure_path_guard_library()
        if path_guard_so is None:
            raise RuntimeError("submission path guard is unavailable")
        env = dict(os.environ)
        if base_env:
            env.update(base_env)
        env["POLYGONLIKE_PATH_GUARD_DENY_PREFIXES"] = "\n".join(deny_prefixes)
        allow_prefixes = self._normalized_path_prefixes(allow_paths)
        if allow_prefixes:
            env["POLYGONLIKE_PATH_GUARD_ALLOW_PREFIXES"] = "\n".join(allow_prefixes)
        else:
            env.pop("POLYGONLIKE_PATH_GUARD_ALLOW_PREFIXES", None)
        existing_ld_preload = str(env.get("LD_PRELOAD") or "").strip()
        if existing_ld_preload:
            env["LD_PRELOAD"] = f"{str(path_guard_so)}:{existing_ld_preload}"
        else:
            env["LD_PRELOAD"] = str(path_guard_so)
        return env

    def _collect_diagnostics(self, workspace: Path | None, text: str) -> list[dict]:
        result: list[dict] = []
        workspace_resolved: Path | None = None
        if workspace is not None:
            try:
                workspace_resolved = workspace.resolve()
            except OSError:
                workspace_resolved = None
        for line in text.splitlines():
            m = DIAG_RE.match(line.strip())
            if not m:
                continue
            file_path = Path(m.group("file"))
            rel = str(file_path)
            can_link = False
            if workspace_resolved is not None:
                try:
                    if file_path.is_absolute():
                        resolved = file_path.resolve()
                    else:
                        resolved = (workspace_resolved / file_path).resolve()
                    rel = str(resolved.relative_to(workspace_resolved))
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

    def _persist_compile_outputs(
        self,
        compile_log_file: Path,
        workspace: Path | None,
        stdout_text: str,
        stderr_text: str,
    ) -> list[dict]:
        diagnostics: list[dict] = []
        wrote_any = False
        saw_stream_text = False
        with compile_log_file.open("w", encoding="utf-8") as clog:
            for chunk in (stdout_text, stderr_text):
                text = str(chunk or "")
                if not text:
                    continue
                saw_stream_text = True
                if wrote_any and not text.startswith("\n"):
                    clog.write("\n")
                clog.write(text)
                if not text.endswith("\n"):
                    clog.write("\n")
                diagnostics.extend(self._collect_diagnostics(workspace, text))
                wrote_any = True
        if not saw_stream_text:
            diagnostics.extend(self._collect_diagnostics(workspace, ""))
        return diagnostics

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
        code = int(rc)
        if code in {0, 42}:
            return "OK"
        if code in {1, 2, 7, 43}:
            return "WA"
        return "FAIL"

    def _normalize_time_limit_ms(self, raw: object) -> int:
        try:
            value = int(raw)
        except Exception:
            value = self.DEFAULT_TIME_LIMIT_MS
        return max(self.TIME_LIMIT_MIN_MS, min(self.TIME_LIMIT_MAX_MS, value))

    def _effective_run_timeout_ms(self, time_limit_ms: int) -> int:
        tl = self._normalize_time_limit_ms(time_limit_ms)
        return max(tl * 2, tl + 1000)

    def _effective_run_timeout_sec(self, run_timeout_ms: int) -> int:
        timeout_ms = max(1, int(run_timeout_ms))
        timeout_sec = max(1, (timeout_ms + 999) // 1000)
        return max(1, min(self.RUN_TIMEOUT_MAX_SEC, timeout_sec))

    def _cap_tle_time_ms(self, time_ms: int, timeout_ms: int) -> int:
        try:
            value = max(0, int(time_ms))
        except Exception:
            value = 0
        try:
            cap = max(1, int(timeout_ms))
        except Exception:
            cap = 0
        if cap > 0 and value > cap:
            return cap
        return value

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
            "time_limit_ms": self.DEFAULT_TIME_LIMIT_MS,
            "run_timeout_ms": self._effective_run_timeout_ms(self.DEFAULT_TIME_LIMIT_MS),
            "run_timeout_sec": 30,
            "run_memory_mb": self.default_run_memory_mb,
            "run_process_limit": self.default_run_process_limit,
            "run_output_kb": self.default_run_output_kb,
        }
        loaded = False
        run_config = artifact_root / "logs" / "run_config.json"
        if run_config.exists():
            try:
                payload = json.loads(run_config.read_text(encoding="utf-8"))
                params = None
                if isinstance(payload, dict):
                    maybe = payload.get("generation_params")
                    if isinstance(maybe, dict):
                        params = maybe
                    else:
                        params = payload
                if isinstance(params, dict):
                    cfg.update(params)
                    loaded = True
            except Exception:
                loaded = False
        if not loaded:
            manifest = artifact_root / "manifest.json"
            if manifest.exists():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    params = payload.get("generation_params")
                    if isinstance(params, dict):
                        cfg.update(params)
                        loaded = True
                        self._write_run_config_sidecar(run_config, params)
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
        time_limit_ms = self._normalize_time_limit_ms(cfg.get("time_limit_ms", self.DEFAULT_TIME_LIMIT_MS))
        run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms)
        run_timeout_sec = self._effective_run_timeout_sec(run_timeout_ms)
        try:
            run_memory_mb = max(16, min(262144, int(cfg.get("run_memory_mb", self.default_run_memory_mb))))
        except Exception:
            run_memory_mb = self.default_run_memory_mb
        try:
            run_process_limit = max(1, min(4096, int(cfg.get("run_process_limit", self.default_run_process_limit))))
        except Exception:
            run_process_limit = self.default_run_process_limit
        try:
            run_output_kb = max(64, min(1048576, int(cfg.get("run_output_kb", self.default_run_output_kb))))
        except Exception:
            run_output_kb = self.default_run_output_kb
        resolved_cfg = {
            "checker_mode": checker_mode,
            "checker_args": [str(x) for x in checker_args],
            "max_passes": max_passes,
            "run_jobs": run_jobs,
            "time_limit_ms": time_limit_ms,
            "run_timeout_ms": run_timeout_ms,
            "run_timeout_sec": run_timeout_sec,
            "run_memory_mb": run_memory_mb,
            "run_process_limit": run_process_limit,
            "run_output_kb": run_output_kb,
        }
        self._cache_put(self._run_config_cache, cache_key, dict(resolved_cfg))
        return dict(resolved_cfg)

    def _write_run_config_sidecar(self, path: Path, params: dict[str, object]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(json.dumps(params, indent=2), encoding="utf-8")
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        except Exception:
            # Sidecar backfill is a best-effort optimization and must not affect run behavior.
            pass

    def _load_test_inputs(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_cache, cache_key)
        if cached is not None:
            return list(cached)
        tests_dir = artifact_root / "tests"
        names = tuple(self._safe_top_level_suffix_names(tests_dir, ".in"))
        self._cache_put(self._test_input_cache, cache_key, names)
        return list(names)

    def _load_answer_files(self, artifact_root: Path) -> list[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._answer_file_cache, cache_key)
        if cached is not None:
            return list(cached)
        ans_dir = artifact_root / "ans"
        names = tuple(self._safe_top_level_suffix_names(ans_dir, ".ans"))
        self._cache_put(self._answer_file_cache, cache_key, names)
        return list(names)

    def _load_answer_file_set(self, artifact_root: Path) -> frozenset[str]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._answer_file_set_cache, cache_key)
        if cached is not None:
            return cached
        names = frozenset(self._load_answer_files(artifact_root))
        self._cache_put(self._answer_file_set_cache, cache_key, names)
        return names

    def _load_test_input_meta(self, artifact_root: Path) -> list[tuple[str, str]]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._test_input_meta_cache, cache_key)
        if cached is not None:
            return list(cached)
        meta = tuple((name, self._test_name_stem(name)) for name in self._load_test_inputs(artifact_root))
        self._cache_put(self._test_input_meta_cache, cache_key, meta)
        return list(meta)

    def _test_name_stem(self, name: str) -> str:
        text = str(name or "")
        # Hot path for cached *.in test discovery names.
        if text.endswith(".in"):
            return text[:-3]
        return os.path.splitext(text)[0]

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

    def _safe_top_level_suffix_names(self, root: Path, suffix: str) -> list[str]:
        if not suffix:
            return []
        matched: list[str] = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith(suffix):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    matched.append(name)
        except OSError:
            return []
        matched.sort()
        return matched

    def _run_interactive_case(
        self,
        interactor_bin: Path,
        submission_bin: Path,
        test: Path,
        ans: Path,
        transcript: Path,
        feedback_dir: Path,
        timeout_sec: int = 30,
        memory_mb: int = 1024,
        process_limit: int = 64,
        output_kb: int = 65536,
    ) -> tuple[str, int, int]:
        sub_err_path = transcript.with_name(f"{transcript.stem}.submission.stderr.txt")
        itr_err_path = transcript.with_name(f"{transcript.stem}.interactor.stderr.txt")
        sub_spec = ExecSpec(
            command=[str(submission_bin)],
            cwd=feedback_dir,
            timeout_sec=timeout_sec,
            memory_mb=memory_mb,
            process_limit=process_limit,
            output_kb=output_kb,
        )
        itr_spec = ExecSpec(
            command=[str(interactor_bin), str(test), str(ans)],
            cwd=feedback_dir,
            timeout_sec=timeout_sec,
            memory_mb=memory_mb,
            process_limit=process_limit,
            output_kb=output_kb,
        )
        with sub_err_path.open("wb") as sub_err_fh, itr_err_path.open("wb") as itr_err_fh:
            sub = self.sandbox.popen(
                sub_spec,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sub_err_fh,
                text=False,
                bufsize=0,
            )
            itr_env = dict(os.environ)
            itr_env["FEEDBACK_DIR"] = str(feedback_dir)
            itr = self.sandbox.popen(
                itr_spec,
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
                    return "TL", elapsed, 0

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
        cwd: Path | None,
        timeout_sec: int,
        time_file: Path,
        memory_mb: int,
        process_limit: int,
        output_kb: int,
        base_env: dict[str, str] | None = None,
        deny_paths: list[Path] | tuple[Path, ...] | None = None,
        allow_paths: list[Path] | tuple[Path, ...] | None = None,
    ) -> tuple[int, int, int]:
        if time_file.exists():
            time_file.unlink()
        if Path("/usr/bin/time").exists():
            cmd = ["/usr/bin/time", "-f", "%M", "-o", str(time_file), str(submission_bin)]
        else:
            cmd = [str(submission_bin)]

        exec_env = self._path_guard_environment(
            base_env=base_env,
            deny_paths=deny_paths,
            allow_paths=allow_paths,
        )

        elapsed_ms = timeout_sec * 1000
        returncode = self.RUN_TIMEOUT_SENTINEL
        exec_result = self.sandbox.run(
            ExecSpec(
                command=cmd,
                cwd=cwd,
                timeout_sec=timeout_sec,
                stdin_path=input_file,
                stdout_path=output_file,
                env=exec_env,
                memory_mb=memory_mb,
                process_limit=process_limit,
                output_kb=output_kb,
            )
        )
        elapsed_ms = exec_result.elapsed_ms
        status = str(exec_result.status or "").strip().lower()
        if exec_result.timed_out or status == "tle":
            returncode = self.RUN_TIMEOUT_SENTINEL
        else:
            raw_rc = exec_result.returncode
            if raw_rc is None:
                returncode = 1
            else:
                returncode = int(raw_rc)
                # /usr/bin/time returns 128+SIGNAL when the wrapped command is terminated by a signal.
                if cmd and cmd[0] == "/usr/bin/time":
                    tle_wrapped_codes = {
                        128 + int(signal.SIGXCPU),
                        128 + int(signal.SIGKILL),
                        128 + int(signal.SIGALRM),
                        128 + int(signal.SIGTERM),
                    }
                    if returncode in tle_wrapped_codes:
                        returncode = self.RUN_TIMEOUT_SENTINEL
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
        wanted_judge = "judgemessage.txt"
        wanted_next = "nextpass.in"
        wanted_team = "teammessage.txt"
        cap = max(1, int(self.FEEDBACK_KEY_FILE_LIMIT))
        keys: list[str] = []
        stop_scan = False
        for dirpath, dirnames, filenames in os.walk(test_feedback_dir, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if base_root_resolved not in dir_root_resolved.parents and base_root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            has_judge = False
            has_next = False
            has_team = False
            for name in filenames:
                if name == wanted_judge:
                    has_judge = True
                elif name == wanted_next:
                    has_next = True
                elif name == wanted_team:
                    has_team = True

            ordered_names: list[str] = []
            if has_judge:
                ordered_names.append(wanted_judge)
            if has_next:
                ordered_names.append(wanted_next)
            if has_team:
                ordered_names.append(wanted_team)

            for name in ordered_names:
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

    def _cap_summary_list_field(
        self,
        payload: dict,
        field: str,
        limit: int,
        truncated_key: str,
        total_key: str,
        limit_key: str,
    ) -> list | None:
        values = payload.get(field)
        if not isinstance(values, list):
            return None
        cap = max(1, int(limit))
        total = len(values)
        payload[limit_key] = cap
        payload[total_key] = total
        if total > cap:
            selected = values[:cap]
            payload[field] = selected
            payload[truncated_key] = True
            return selected
        payload[truncated_key] = False
        return values

    def _cap_run_test_feedback_files(self, tests: list) -> list:
        cap = max(1, int(self.DB_SUMMARY_FEEDBACK_FILES_LIMIT))
        normalized: list = []
        for raw in tests:
            if not isinstance(raw, dict):
                normalized.append(raw)
                continue
            row = dict(raw)
            feedback_files = row.get("feedback_files")
            if isinstance(feedback_files, list):
                total = len(feedback_files)
                row["feedback_files_limit"] = cap
                row["feedback_files_total"] = total
                if total > cap:
                    row["feedback_files"] = feedback_files[:cap]
                    row["feedback_files_truncated"] = True
                else:
                    row["feedback_files_truncated"] = False
            normalized.append(row)
        return normalized

    def _summary_for_db(self, summary: dict) -> str:
        payload = dict(summary)
        tests = self._cap_summary_list_field(
            payload,
            "tests",
            self.DB_SUMMARY_TESTS_LIMIT,
            "tests_truncated",
            "tests_total",
            "tests_limit",
        )
        if isinstance(tests, list):
            payload["tests"] = self._cap_run_test_feedback_files(tests)
        self._cap_summary_list_field(
            payload,
            "compile_diagnostics",
            self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            "compile_diagnostics_truncated",
            "compile_diagnostics_total",
            "compile_diagnostics_limit",
        )
        diagnostics = payload.get("compile_diagnostics")
        if isinstance(diagnostics, list):
            payload["compile_diagnostics"] = self._normalize_diagnostics_for_db(
                diagnostics,
                self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
            )
        return json.dumps(payload)

    def _truncate_inline_text(self, value: str, max_chars: int) -> tuple[str, bool]:
        cap = max(1, int(max_chars))
        text = str(value or "")
        if len(text) <= cap:
            return text, False
        return text[:cap] + f"... [truncated; showing first {cap} characters]", True

    def _normalize_diagnostics_for_db(self, entries: list, message_limit: int) -> list[dict]:
        normalized: list[dict] = []
        cap = max(1, int(message_limit))
        for raw in entries:
            item = raw if isinstance(raw, dict) else {"message": str(raw or "")}
            msg, msg_truncated = self._truncate_inline_text(str(item.get("message") or ""), cap)
            row = dict(item)
            row["message"] = msg
            row["message_truncated"] = bool(msg_truncated)
            row["message_limit"] = cap
            row.setdefault("level", "error")
            row.setdefault("file", "")
            row.setdefault("line", 0)
            row.setdefault("column", 0)
            row.setdefault("can_link", False)
            normalized.append(row)
        return normalized

    def _finalize_run(self, run_id: str, status: str, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, summary_json=?, finished_at=? WHERE id=?",
            [status, self._summary_for_db(summary), now_iso(), run_id],
        )

    def _run_noninteractive_test(
        self,
        mode: str,
        sub_bin: Path,
        checker: Path,
        checker_mode: str,
        checker_args: list[str],
        max_passes: int,
        run_timeout_sec: int,
        run_timeout_ms: int,
        run_memory_mb: int,
        run_process_limit: int,
        run_output_kb: int,
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
            "sandbox_status": "ok",
            "time_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        current_input = test
        pass_idx = 1
        total_time = 0
        peak_mem = 0
        # Submission should not read generated answers directly from build artifacts.
        guard_deny_paths = [ans.parent]
        guard_allow_paths = [run_root]
        while True:
            pass_feedback_dir = test_feedback_dir / f"pass{pass_idx}"
            pass_feedback_dir.mkdir(parents=True, exist_ok=True)
            out = run_root / f"{test.stem}.pass{pass_idx}.out"
            time_file = pass_feedback_dir / "time.txt"
            exec_rc, exec_ms, exec_mem = self._run_pass(
                sub_bin,
                current_input,
                out,
                run_root,
                run_timeout_sec,
                time_file,
                run_memory_mb,
                run_process_limit,
                run_output_kb,
                deny_paths=guard_deny_paths,
                allow_paths=guard_allow_paths,
            )
            # Some resource-limit exits (for example output file size) may return non-zero
            # after already consuming timeout budget. Treat these as timeout for verdict
            # consistency when elapsed runtime has crossed run timeout threshold.
            if exec_rc != self.RUN_TIMEOUT_SENTINEL and run_timeout_ms > 0:
                if int(exec_ms) >= int(run_timeout_ms):
                    exec_rc = self.RUN_TIMEOUT_SENTINEL
            total_time += exec_ms
            peak_mem = max(peak_mem, exec_mem)
            if exec_rc == self.RUN_TIMEOUT_SENTINEL:
                capped_exec_ms = self._cap_tle_time_ms(exec_ms, run_timeout_ms)
                if capped_exec_ms != exec_ms:
                    total_time = max(0, total_time - (exec_ms - capped_exec_ms))
                p = {"pass": pass_idx, "verdict": "TL", "time_ms": capped_exec_ms, "memory_kb": exec_mem}
                test_result["passes"].append(p)
                test_result["verdict"] = "TL"
                test_result["sandbox_status"] = "tle"
                break
            if exec_rc != 0:
                p = {"pass": pass_idx, "verdict": "RE", "time_ms": exec_ms, "memory_kb": exec_mem}
                test_result["passes"].append(p)
                test_result["verdict"] = "RE"
                test_result["sandbox_status"] = "re"
                break

            checker_verdict = "OK"
            if checker.exists():
                env = dict(os.environ)
                env["FEEDBACK_DIR"] = str(pass_feedback_dir)
                checker_log = ""
                checker_timed_out = False
                if checker_mode == "kattis":
                    feedback_arg = str(pass_feedback_dir) + os.sep
                    check_result = self.sandbox.run(
                        ExecSpec(
                            command=[str(checker), str(test), str(ans), feedback_arg, *checker_args],
                            stdin_path=out,
                            timeout_sec=run_timeout_sec,
                            cwd=pass_feedback_dir,
                            env=env,
                            memory_mb=run_memory_mb,
                            process_limit=run_process_limit,
                            output_kb=run_output_kb,
                        )
                    )
                    checker_log = (check_result.stdout or "") + (check_result.stderr or "")
                    checker_timed_out = bool(check_result.timed_out)
                else:
                    check_result = self.sandbox.run(
                        ExecSpec(
                            command=[str(checker), str(test), str(out), str(ans), *checker_args],
                            timeout_sec=run_timeout_sec,
                            cwd=pass_feedback_dir,
                            env=env,
                            memory_mb=run_memory_mb,
                            process_limit=run_process_limit,
                            output_kb=run_output_kb,
                        )
                    )
                    checker_log = (check_result.stdout or "") + (check_result.stderr or "")
                    checker_timed_out = bool(check_result.timed_out)
                (pass_feedback_dir / "checker.log").write_text(checker_log, encoding="utf-8")
                if checker_timed_out:
                    checker_verdict = "FAIL"
                else:
                    checker_verdict = self._validator_style_verdict(int(check_result.returncode or 0))
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

        if str(test_result.get("verdict") or "").strip().upper().startswith("TL"):
            total_time = self._cap_tle_time_ms(total_time, run_timeout_ms)
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
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
    ) -> str:
        supported_modes = {"pass-fail", "interactive", "multi-pass"}
        raw_selected_tests: list[str] = []
        if isinstance(selected_tests, str):
            raw_selected_tests.append(selected_tests)
        elif isinstance(selected_tests, list):
            raw_selected_tests.extend(str(item or "") for item in selected_tests)
        elif isinstance(selected_tests, tuple):
            raw_selected_tests.extend(str(item or "") for item in selected_tests)
        elif selected_tests is not None:
            try:
                raw_selected_tests.extend(str(item or "") for item in list(selected_tests))
            except Exception:
                raw_selected_tests.append(str(selected_tests or ""))
        selected_test_names: list[str] = []
        seen_selected_test_names: set[str] = set()
        for raw in raw_selected_tests:
            token = str(raw or "").strip()
            if not token:
                continue
            if not RUN_TEST_NAME_RE.fullmatch(token):
                raise RuntimeError(f"invalid selected test name: {token}")
            if token in seen_selected_test_names:
                continue
            seen_selected_test_names.add(token)
            selected_test_names.append(token)

        run_id = str(run_id or "").strip() or f"r-{uuid.uuid4().hex[:12]}"
        safe_invocation_id = str(invocation_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", safe_invocation_id):
            safe_invocation_id = ""
        safe_invocation_run_ids: list[str] = []
        raw_invocation_run_ids = invocation_run_ids or []
        for raw in raw_invocation_run_ids:
            token = str(raw or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", token):
                continue
            if token in safe_invocation_run_ids:
                continue
            safe_invocation_run_ids.append(token)
        if safe_invocation_id and run_id not in safe_invocation_run_ids:
            safe_invocation_run_ids.append(run_id)
        safe_expected_behavior = str(expected_behavior or "").strip().lower()
        if safe_expected_behavior not in {
            "accepted",
            "wrong_answer",
            "time_limit_exceeded",
            "runtime_error",
            "failed",
            "unknown",
        }:
            safe_expected_behavior = "unknown"
        safe_invocation_source = str(invocation_source or "run.execute").strip() or "run.execute"

        def _attach_invocation_block(summary: dict, *, completed: bool | None = None) -> None:
            if not safe_invocation_id or not isinstance(summary, dict):
                return
            payload: dict[str, object] = {
                "id": safe_invocation_id,
                "source": safe_invocation_source,
                "run_ids": list(safe_invocation_run_ids),
                "expected_behavior": safe_expected_behavior,
            }
            if isinstance(completed, bool):
                payload["completed"] = completed
            summary["invocation"] = payload

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
        if safe_invocation_id:
            initial_summary = {
                "mode": mode,
                "source": submission_path or upload_filename or "upload",
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "tests": [],
                "compile_log": "compile.log",
                "compile_diagnostics": [],
                "limits": {},
                "usage": {},
            }
            _attach_invocation_block(initial_summary, completed=False)
            self.db.execute("UPDATE runs SET summary_json=? WHERE id=?", [self._summary_for_db(initial_summary), run_id])

        if not preflight_ok:
            error = preflight_error
            compile_log_file.write_text(error + "\n", encoding="utf-8")
            summary = {
                "error": error,
                "mode": mode,
                "source": submission_path or "upload",
                "tests": [],
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "compile_log": "compile.log",
                "compile_diagnostics": [],
                "toolchain_digest": "unknown",
                "sandbox_backend": self.sandbox.name,
                "limits": {},
                "usage": {},
            }
            _attach_invocation_block(summary, completed=True)
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._finalize_run(run_id, "failed", summary)
            return run_id

        assert artifact_root is not None
        run_cfg = self._load_run_config(artifact_root)
        checker_mode = str(run_cfg["checker_mode"])
        checker_args = [str(x) for x in run_cfg["checker_args"]]
        max_passes = int(run_cfg["max_passes"])
        run_timeout_ms = int(run_cfg.get("run_timeout_ms") or 0)
        run_timeout_sec = int(run_cfg["run_timeout_sec"])
        run_memory_mb = int(run_cfg["run_memory_mb"])
        run_process_limit = int(run_cfg["run_process_limit"])
        run_output_kb = int(run_cfg["run_output_kb"])
        sandbox_limits = {
            "cpu_ms": run_timeout_ms,
            "memory_mb": run_memory_mb,
            "pids": run_process_limit,
            "output_kb": run_output_kb,
        }

        def _persist_running_summary(current_tests: list[dict]) -> None:
            tests_snapshot = [dict(row) for row in current_tests if isinstance(row, dict)]
            summary = {
                "mode": mode,
                "source": source_label,
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "tests": tests_snapshot,
                "feedback_dir": "feedback_dir",
                "compile_diagnostics": compile_diagnostics,
                "compile_log": "compile.log",
                "toolchain_digest": toolchain_digest,
                "run_config": run_cfg,
                "sandbox_backend": self.sandbox.name,
                "limits": sandbox_limits,
                "usage": {
                    "tests": len(tests_snapshot),
                    "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in tests_snapshot if isinstance(t, dict)),
                    "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in tests_snapshot if isinstance(t, dict)] or [0]),
                },
            }
            _attach_invocation_block(summary, completed=False)
            self.db.execute("UPDATE runs SET summary_json=? WHERE id=?", [self._summary_for_db(summary), run_id])

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
                source_in_workspace: Path | None = None
                with self.workspace_service.workspace_lock(workspace):
                    source_in_workspace = self._resolve_submission_source(workspace, submission_path)
                    sub_src = run_root / source_in_workspace.name
                    shutil.copy2(source_in_workspace, sub_src)
                source_label = submission_path
                compile_workspace = None

            include_dirs: list[Path] = []
            include_dir = workspace / "third_party" / "testlib"
            if include_dir.exists():
                include_dirs.append(include_dir)

            ok, cout, cerr, toolchain_digest = self.toolchain.compile_program(
                sub_src,
                sub_bin,
                include_dirs,
                path_roots=[run_root, workspace],
                cxxflags=list(self.SUBMISSION_CPP_CXXFLAGS),
            )

            compile_diagnostics = self._persist_compile_outputs(
                compile_log_file,
                compile_workspace,
                cout,
                cerr,
            )
            if not ok:
                summary = {
                    "error": "compile_error",
                    "compile_log": "compile.log",
                    "compile_diagnostics": compile_diagnostics,
                    "toolchain_digest": toolchain_digest,
                    "source": source_label,
                    "selected_tests": selected_test_names,
                    "selected_tests_count": len(selected_test_names),
                    "run_config": run_cfg,
                    "sandbox_backend": self.sandbox.name,
                    "limits": sandbox_limits,
                    "usage": {},
                }
                _attach_invocation_block(summary, completed=True)
                (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
                self._finalize_run(run_id, "failed", summary)
                return run_id

            test_meta = self._load_test_input_meta(artifact_root)
            if not test_meta:
                raise RuntimeError("selected build has no tests")
            if selected_test_names:
                test_meta_by_name = {name: (name, stem) for name, stem in test_meta}
                missing_tests = [name for name in selected_test_names if name not in test_meta_by_name]
                if missing_tests:
                    shown = ", ".join(missing_tests[:6])
                    if len(missing_tests) > 6:
                        shown += ", ..."
                    raise RuntimeError(f"selected tests missing in build: {shown}")
                test_meta = [test_meta_by_name[name] for name in selected_test_names]
            answer_names = self._load_answer_file_set(artifact_root)
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
                        "sandbox_status": "ok",
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
                        memory_mb=run_memory_mb,
                        process_limit=run_process_limit,
                        output_kb=run_output_kb,
                    )
                    if str(verdict or "").strip().upper().startswith("TL"):
                        elapsed = self._cap_tle_time_ms(elapsed, run_timeout_ms)
                    test_result["passes"].append({"pass": 1, "verdict": verdict, "time_ms": elapsed, "memory_kb": mem_kb})
                    test_result["verdict"] = verdict
                    if str(verdict or "").strip().upper().startswith("TL"):
                        test_result["sandbox_status"] = "tle"
                    elif verdict == "RE":
                        test_result["sandbox_status"] = "re"
                    test_result["time_ms"] = elapsed
                    test_result["memory_kb"] = mem_kb
                    test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
                    test_result["transcript"] = str(transcript.relative_to(run_root))
                    verdicts.append(test_result)
                    _persist_running_summary(verdicts)
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
                            run_timeout_ms,
                            run_memory_mb,
                            run_process_limit,
                            run_output_kb,
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
                        current_progress = [row for row in parallel_verdicts if isinstance(row, dict) and row]
                        _persist_running_summary(current_progress)
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
                            run_timeout_ms,
                            run_memory_mb,
                            run_process_limit,
                            run_output_kb,
                            test,
                            ans,
                            feedback_dir / test_stem,
                            run_root,
                        )
                    )
                    _persist_running_summary(verdicts)

            summary = {
                "mode": mode,
                "source": source_label,
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "tests": verdicts,
                "feedback_dir": "feedback_dir",
                "compile_diagnostics": compile_diagnostics,
                "compile_log": "compile.log",
                "toolchain_digest": toolchain_digest,
                "run_config": run_cfg,
                "sandbox_backend": self.sandbox.name,
                "limits": sandbox_limits,
                "usage": {
                    "tests": len(verdicts),
                    "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                    "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in verdicts if isinstance(t, dict)] or [0]),
                },
            }
            _attach_invocation_block(summary, completed=True)
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._finalize_run(run_id, "ok", summary)
        except Exception as exc:
            if not compile_log_file.exists():
                compile_log_file.write_text(str(exc) + "\n", encoding="utf-8")
            summary = {
                "error": str(exc),
                "mode": mode,
                "source": source_label,
                "selected_tests": selected_test_names,
                "selected_tests_count": len(selected_test_names),
                "tests": verdicts,
                "feedback_dir": "feedback_dir",
                "compile_diagnostics": compile_diagnostics,
                "compile_log": "compile.log",
                "toolchain_digest": toolchain_digest,
                "run_config": run_cfg,
                "sandbox_backend": self.sandbox.name,
                "limits": sandbox_limits,
                "usage": {
                    "tests": len(verdicts),
                    "time_ms_total": sum(int(t.get("time_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                    "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in verdicts if isinstance(t, dict)] or [0]),
                },
            }
            _attach_invocation_block(summary, completed=True)
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._finalize_run(run_id, "failed", summary)

        return run_id
