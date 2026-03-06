from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO

from app.db import DB, now_iso
from app.services.fs_manager import FsManager
from app.runtime_values import RuntimeValues, build_runtime_values
from app.services.solution_metadata import normalize_expected_behavior
from app.services.sandbox import ExecSpec, SandboxBackend, NativeSandboxBackend
from app.services.toolchain_service import ToolchainService
from app.services.util import run_cmd
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
    RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS = 750
    RUN_PROGRESS_PERSIST_BATCH_UPDATES = 4
    DEFAULT_TIME_LIMIT_MS = 2000
    TIME_LIMIT_MIN_MS = 100
    TIME_LIMIT_MAX_MS = 30000
    RUN_TIMEOUT_MAX_SEC = 300
    SUBMISSION_CPP_CXXFLAGS = ["-O2", "-std=gnu++20", "-pipe", "-DDOMJUDGE"]
    PATH_GUARD_SOURCE = (Path(__file__).resolve().parent / "sandbox" / "path_guard.c").resolve()

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        toolchain: ToolchainService,
        sandbox_backend: SandboxBackend | None = None,
        constants: RuntimeValues | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.toolchain = toolchain
        self.sandbox = sandbox_backend or NativeSandboxBackend()
        self.default_run_memory_mb = 1024
        self.default_run_process_limit = 64
        self.default_run_output_kb = 65536
        self.wall_time_slack_pass_fail_sec = 1
        self.wall_time_slack_multi_pass_sec = 15
        self.wall_time_slack_interactive_sec = 15
        self._run_config_cache: dict[str, dict[str, object]] = {}
        self._test_input_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_cache: dict[str, tuple[str, ...]] = {}
        self._answer_file_set_cache: dict[str, frozenset[str]] = {}
        self._test_input_meta_cache: dict[str, tuple[tuple[str, str], ...]] = {}
        self.fs_manager = FsManager(self.workspace_service.settings.artifacts_root, self.workspace_service.settings.run_root)
        self._cache_lock = threading.Lock()
        self._path_guard_lock = threading.Lock()
        self.apply_runtime_values(constants or build_runtime_values())

    def _coerce_int(self, raw: object, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw)
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.default_run_memory_mb = self._coerce_int(
            values.get("RUN_EXEC_MEMORY_MB", 1024),
            default=1024,
            min_value=16,
            max_value=262144,
        )
        self.default_run_process_limit = self._coerce_int(
            values.get("RUN_EXEC_PROCESS_LIMIT", 64),
            default=64,
            min_value=1,
            max_value=4096,
        )
        self.default_run_output_kb = self._coerce_int(
            values.get("RUN_EXEC_OUTPUT_KB", 65536),
            default=65536,
            min_value=64,
            max_value=1048576,
        )
        self.wall_time_slack_pass_fail_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_PASS_FAIL_SEC", 1),
            default=1,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_multi_pass_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_MULTI_PASS_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )
        self.wall_time_slack_interactive_sec = self._coerce_int(
            values.get("RUN_WALL_TIME_SLACK_INTERACTIVE_SEC", 15),
            default=15,
            min_value=0,
            max_value=300,
        )
        with self._cache_lock:
            self._run_config_cache.clear()

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
        if code == 42:
            return "OK"
        if code == 43:
            return "WA"
        return "FL"

    def _run_checker(
        self,
        checker: Path,
        *,
        checker_args: list[str],
        test: Path,
        team_output: Path,
        answer: Path,
        pass_feedback_dir: Path,
        run_timeout_sec: int,
        run_memory_mb: int,
        run_process_limit: int,
        run_output_kb: int,
    ) -> tuple[object, str, bool]:
        env = dict(os.environ)
        env["FEEDBACK_DIR"] = str(pass_feedback_dir)
        feedback_arg = str(pass_feedback_dir) + os.sep
        check_result = self.sandbox.run(
            ExecSpec(
                command=[str(checker), str(test), str(answer), feedback_arg, *checker_args],
                stdin_path=team_output,
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
        return check_result, checker_log, checker_timed_out

    def _normalize_time_limit_ms(self, raw: object) -> int:
        try:
            value = int(raw)
        except Exception:
            value = self.DEFAULT_TIME_LIMIT_MS
        return max(self.TIME_LIMIT_MIN_MS, min(self.TIME_LIMIT_MAX_MS, value))

    def _normalize_problem_mode(self, raw: object, default: str = "pass-fail") -> str:
        token = str(raw or "").strip().lower()
        if token in {"pass-fail", "interactive", "multi-pass"}:
            return token
        return default

    def _wall_time_slack_sec_for_mode(self, mode: object) -> int:
        token = self._normalize_problem_mode(mode)
        if token == "interactive":
            return max(0, int(self.wall_time_slack_interactive_sec))
        if token == "multi-pass":
            return max(0, int(self.wall_time_slack_multi_pass_sec))
        return max(0, int(self.wall_time_slack_pass_fail_sec))

    def _effective_run_timeout_ms(self, time_limit_ms: int, *, mode: object = "pass-fail") -> int:
        tl = self._normalize_time_limit_ms(time_limit_ms)
        slack_ms = self._wall_time_slack_sec_for_mode(mode) * 1000
        return max(1, tl * 2 + slack_ms)

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

    def _time_value_to_ms(self, token: object) -> int:
        raw = str(token or "").strip().replace(",", ".")
        if not raw:
            return 0
        try:
            return max(0, int(float(raw) * 1000.0))
        except Exception:
            return 0

    def _read_time_metrics(self, time_file: Path) -> tuple[int, int]:
        if not time_file.exists():
            return (0, 0)
        try:
            tokens = time_file.read_text(encoding="utf-8", errors="replace").strip().split()
        except OSError:
            return (0, 0)
        if not tokens:
            return (0, 0)
        mem_kb = 0
        user_ms = 0
        if len(tokens) >= 3 and str(tokens[0]).isdigit():
            mem_kb = int(tokens[0])
            user_ms = self._time_value_to_ms(tokens[1]) + self._time_value_to_ms(tokens[2])
            return (mem_kb, user_ms)
        if len(tokens) >= 2:
            user_ms = self._time_value_to_ms(tokens[0]) + self._time_value_to_ms(tokens[1])
            return (0, user_ms)
        if str(tokens[0]).isdigit():
            mem_kb = int(tokens[0])
        return (mem_kb, 0)

    def _load_run_config(self, artifact_root: Path, *, default_mode: object = "pass-fail") -> dict[str, object]:
        cache_key = self._artifact_cache_key(artifact_root)
        cached = self._cache_get(self._run_config_cache, cache_key)
        if cached is not None:
            return dict(cached)

        normalized_default_mode = self._normalize_problem_mode(default_mode, "pass-fail")
        cfg: dict[str, object] = {
            "checker_args": [],
            "max_passes": 16,
            "run_jobs": 0,
            "mode": normalized_default_mode,
            "time_limit_ms": self.DEFAULT_TIME_LIMIT_MS,
            "run_timeout_ms": self._effective_run_timeout_ms(self.DEFAULT_TIME_LIMIT_MS, mode=normalized_default_mode),
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
        mode = self._normalize_problem_mode(cfg.get("mode"), normalized_default_mode)
        time_limit_ms = self._normalize_time_limit_ms(cfg.get("time_limit_ms", self.DEFAULT_TIME_LIMIT_MS))
        run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms, mode=mode)
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
            "checker_args": [str(x) for x in checker_args],
            "max_passes": max_passes,
            "run_jobs": run_jobs,
            "mode": mode,
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

    def _canonical_build_artifact_root(self, build_ref: str) -> Path:
        safe_ref = str(build_ref or "").strip().lower()
        try:
            root = self.fs_manager.build_paths(safe_ref).root.resolve()
        except Exception as exc:
            raise RuntimeError("invalid build ref") from exc
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
        interactor_output: Path,
        transcript: Path,
        feedback_dir: Path,
        timeout_sec: int = 30,
        timeout_ms: int = 0,
        memory_mb: int = 1024,
        process_limit: int = 64,
        output_kb: int = 65536,
    ) -> tuple[str, int, int, int]:
        sub_err_path = transcript.with_name(f"{transcript.stem}.submission.stderr.txt")
        itr_err_path = transcript.with_name(f"{transcript.stem}.interactor.stderr.txt")
        sub_cwd = feedback_dir / "submission"
        itr_cwd = feedback_dir / "interactor"
        sub_cwd.mkdir(parents=True, exist_ok=True)
        itr_cwd.mkdir(parents=True, exist_ok=True)
        # The sandbox filesystem is read-only outside the execution cwd; keep
        # /usr/bin/time output inside submission cwd to avoid write failures.
        sub_time_path = sub_cwd / "submission.time.txt"
        itr_input = itr_cwd / "input.in"
        itr_answer = itr_cwd / "answer.ans"
        itr_output = itr_cwd / "output.ans"
        shutil.copy2(test, itr_input)
        shutil.copy2(ans, itr_answer)
        itr_output.unlink(missing_ok=True)
        interactor_output.unlink(missing_ok=True)
        sub_time_path.unlink(missing_ok=True)
        if Path("/usr/bin/time").exists():
            sub_cmd = ["/usr/bin/time", "-f", "%U %S", "-o", sub_time_path.name, str(submission_bin)]
        else:
            sub_cmd = [str(submission_bin)]
        sub_spec = ExecSpec(
            command=sub_cmd,
            cwd=sub_cwd,
            timeout_sec=timeout_sec,
            memory_mb=memory_mb,
            process_limit=process_limit,
            output_kb=output_kb,
        )
        itr_spec = ExecSpec(
            command=[str(interactor_bin), itr_input.name, itr_output.name, itr_answer.name],
            cwd=itr_cwd,
            timeout_sec=timeout_sec,
            memory_mb=memory_mb,
            process_limit=process_limit,
            output_kb=output_kb,
        )
        with sub_err_path.open("wb") as sub_err_fh, itr_err_path.open("wb") as itr_err_fh:
            itr_to_sub_r = -1
            itr_to_sub_w = -1
            sub_to_itr_r = -1
            sub_to_itr_w = -1
            sub: subprocess.Popen[bytes] | None = None
            itr: subprocess.Popen[bytes] | None = None
            itr_env = dict(os.environ)
            itr_env["FEEDBACK_DIR"] = str(itr_cwd)
            try:
                # Full-duplex direct pipes:
                # interactor stdout -> submission stdin
                # submission stdout -> interactor stdin
                itr_to_sub_r, itr_to_sub_w = os.pipe()
                sub_to_itr_r, sub_to_itr_w = os.pipe()
                sub = self.sandbox.popen(
                    sub_spec,
                    stdin=itr_to_sub_r,
                    stdout=sub_to_itr_w,
                    stderr=sub_err_fh,
                    text=False,
                    bufsize=0,
                )
                itr = self.sandbox.popen(
                    itr_spec,
                    stdin=sub_to_itr_r,
                    stdout=itr_to_sub_w,
                    stderr=itr_err_fh,
                    text=False,
                    bufsize=0,
                    env=itr_env,
                )
            finally:
                for fd in (itr_to_sub_r, itr_to_sub_w, sub_to_itr_r, sub_to_itr_w):
                    if fd < 0:
                        continue
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            with transcript.open("w", encoding="utf-8") as tf:
                tf.write("interactive bridge: direct pipe\n")
                if sub is None or itr is None:
                    tf.write("process setup failed\n")
                    return "FL", 0, 0, 0

                start = time.monotonic()
                timed_out = False
                try:
                    while True:
                        if time.monotonic() - start > timeout_sec:
                            timed_out = True
                            if sub.poll() is None:
                                sub.kill()
                            if itr.poll() is None:
                                itr.kill()
                            break
                        if sub.poll() is not None and itr.poll() is not None:
                            break
                        time.sleep(0.01)
                finally:
                    for stream in (sub.stdin, sub.stdout, itr.stdin, itr.stdout):
                        if stream is None:
                            continue
                        try:
                            stream.close()
                        except Exception:
                            pass

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
                wall_ms = int((time.monotonic() - start) * 1000)
                _sub_mem_kb, user_ms = self._read_time_metrics(sub_time_path)
                if user_ms <= 0:
                    user_ms = 0
                if timed_out:
                    tf.write("timeout\n")
                    if timeout_ms > 0:
                        if user_ms <= 0:
                            user_ms = timeout_ms
                        else:
                            user_ms = self._cap_tle_time_ms(max(user_ms, timeout_ms), timeout_ms)
                    return "TL", user_ms, wall_ms, 0

                if sub.returncode != 0:
                    err = sub_err_path.read_text(encoding="utf-8", errors="replace") if sub_err_path.exists() else ""
                    tf.write(f"submission stderr:\n{err}\n")
                    if timeout_ms > 0 and user_ms >= timeout_ms:
                        return "TL", self._cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
                    return "RE", user_ms, wall_ms, 0
                itr_verdict = self._validator_style_verdict(itr.returncode or 0)
                if itr_verdict != "OK":
                    err = itr_err_path.read_text(encoding="utf-8", errors="replace") if itr_err_path.exists() else ""
                    tf.write(f"interactor stderr:\n{err}\n")
                    if timeout_ms > 0 and user_ms >= timeout_ms:
                        return "TL", self._cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
                    return itr_verdict, user_ms, wall_ms, 0
                if timeout_ms > 0 and user_ms >= timeout_ms:
                    return "TL", self._cap_tle_time_ms(user_ms, timeout_ms), wall_ms, 0
                if itr_output.exists():
                    shutil.copy2(itr_output, interactor_output)
                return "OK", user_ms, wall_ms, 0

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
    ) -> tuple[int, int, int, int]:
        if time_file.exists():
            time_file.unlink()
        if Path("/usr/bin/time").exists():
            cmd = ["/usr/bin/time", "-f", "%M %U %S", "-o", str(time_file), str(submission_bin)]
        else:
            cmd = [str(submission_bin)]

        exec_env = self._path_guard_environment(
            base_env=base_env,
            deny_paths=deny_paths,
            allow_paths=allow_paths,
        )

        wall_ms = timeout_sec * 1000
        user_ms = timeout_sec * 1000
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
        wall_ms = exec_result.elapsed_ms
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
        mem_kb, parsed_user_ms = self._read_time_metrics(time_file)
        if parsed_user_ms > 0:
            user_ms = parsed_user_ms
        else:
            user_ms = 0
        return returncode, user_ms, wall_ms, mem_kb

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
            has_team = False
            for name in filenames:
                if name == wanted_judge:
                    has_judge = True
                elif name == wanted_team:
                    has_team = True

            ordered_names: list[str] = []
            if has_judge:
                ordered_names.append(wanted_judge)
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

    def _feedback_message_for_pass(self, pass_feedback_dir: Path, base_root: Path) -> str:
        candidates = ("judgemessage.txt", "teammessage.txt", "checker.log")
        for name in candidates:
            path = pass_feedback_dir / name
            if not self._is_safe_regular_file(base_root, path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                line = self._compact_inline_error(raw_line)
                if line:
                    return line
        return ""

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

    def _compact_inline_error(self, raw: object, *, max_chars: int = 240) -> str:
        text = " ".join(str(raw or "").split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    def _build_failure_context(self, build_row: object) -> tuple[str, str]:
        if build_row is None:
            return ("", "")
        status = ""
        summary_raw = ""
        try:
            status = str(build_row["status"] or "").strip().lower()
        except Exception:
            status = ""
        try:
            summary_raw = str(build_row["summary_json"] or "")
        except Exception:
            summary_raw = ""
        summary_obj: dict = {}
        if summary_raw:
            try:
                parsed = json.loads(summary_raw)
                if isinstance(parsed, dict):
                    summary_obj = parsed
            except Exception:
                summary_obj = {}
        failed_test_raw = str(summary_obj.get("failed_test") or "").strip()
        failed_test = ""
        if failed_test_raw:
            test_name = Path(failed_test_raw).name
            if RUN_TEST_NAME_RE.fullmatch(test_name):
                failed_test = test_name
        failed_step = str(summary_obj.get("failed_step") or "").strip()
        build_error = self._compact_inline_error(summary_obj.get("error"))
        reason = ""
        if build_error:
            reason = build_error
        elif failed_step and failed_test_raw:
            reason = self._compact_inline_error(f"{failed_step} failed on {failed_test_raw}")
        elif failed_step:
            reason = self._compact_inline_error(f"{failed_step} failed")
        elif status and status != "ok":
            reason = f"build status is {status}"
        return (failed_test, reason)

    def _synthesize_failure_tests(
        self,
        *,
        preferred_test: str = "",
        selected_test_names: list[str] | None = None,
        reason: str = "",
    ) -> list[dict]:
        candidates: list[str] = []
        if preferred_test:
            candidates.append(preferred_test)
        for item in selected_test_names or []:
            candidates.append(str(item or ""))
        candidates.append("001.in")
        test_name = ""
        for raw in candidates:
            token = Path(str(raw or "").strip()).name
            if RUN_TEST_NAME_RE.fullmatch(token):
                test_name = token
                break
        if not test_name:
            return []
        feedback = self._compact_inline_error(reason)
        pass_row: dict[str, object] = {
            "pass": 1,
            "verdict": "FL",
            "time_ms": 0,
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
        }
        if feedback:
            pass_row["feedback"] = feedback
        test_row: dict[str, object] = {
            "test": test_name,
            "passes": [pass_row],
            "verdict": "FL",
            "sandbox_status": "fail",
            "time_ms": 0,
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        if feedback:
            test_row["message"] = feedback
        return [test_row]

    def _is_fl_verdict(self, verdict: object) -> bool:
        token = str(verdict or "").strip().upper()
        return token in {"FL", "FAIL", "FAILED"}

    def _synthesized_fl_skip_test(self, test_name: str, *, caused_by_test: str) -> dict[str, object]:
        reason = f"fail due to test {caused_by_test}"
        pass_row: dict[str, object] = {
            "pass": 1,
            "verdict": "FL",
            "time_ms": 0,
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
            "feedback": reason,
        }
        return {
            "test": test_name,
            "passes": [pass_row],
            "verdict": "FL",
            "sandbox_status": "fail",
            "time_ms": 0,
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
            "message": reason,
        }

    def _append_fl_skip_tail_tests(
        self,
        *,
        verdicts: list[dict],
        test_meta: list[tuple[str, str]],
        failed_index: int,
        caused_by_test: str,
    ) -> None:
        for rem_name, _rem_stem in test_meta[failed_index + 1 :]:
            verdicts.append(self._synthesized_fl_skip_test(rem_name, caused_by_test=caused_by_test))

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
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        current_input = test
        pass_idx = 1
        total_user_ms = 0
        total_wall_ms = 0
        peak_mem = 0
        # Submission should not read generated answers directly from build artifacts.
        guard_deny_paths = [ans.parent]
        guard_allow_paths = [run_root]
        final_pass_row: dict[str, object] | None = None
        while True:
            pass_feedback_dir = test_feedback_dir
            pass_feedback_dir.mkdir(parents=True, exist_ok=True)
            out = run_root / f"{test.stem}.out"
            time_file = pass_feedback_dir / "time.txt"
            exec_rc, exec_user_ms, exec_wall_ms, exec_mem = self._run_pass(
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
            # consistency when user runtime has crossed run timeout threshold.
            if exec_rc != self.RUN_TIMEOUT_SENTINEL and run_timeout_ms > 0:
                if int(exec_user_ms) >= int(run_timeout_ms):
                    exec_rc = self.RUN_TIMEOUT_SENTINEL
            total_user_ms += exec_user_ms
            total_wall_ms += exec_wall_ms
            peak_mem = max(peak_mem, exec_mem)
            if exec_rc == self.RUN_TIMEOUT_SENTINEL:
                capped_user_ms = self._cap_tle_time_ms(exec_user_ms, run_timeout_ms)
                if capped_user_ms != exec_user_ms:
                    total_user_ms = max(0, total_user_ms - (exec_user_ms - capped_user_ms))
                p = {
                    "verdict": "TL",
                    "time_ms": capped_user_ms,
                    "time_user_ms": capped_user_ms,
                    "time_wall_ms": exec_wall_ms,
                    "memory_kb": exec_mem,
                }
                pass_feedback = self._feedback_message_for_pass(pass_feedback_dir, run_root)
                if pass_feedback:
                    p["feedback"] = pass_feedback
                final_pass_row = p
                test_result["verdict"] = "TL"
                test_result["sandbox_status"] = "tle"
                break
            if exec_rc != 0:
                p = {
                    "verdict": "RE",
                    "time_ms": exec_user_ms,
                    "time_user_ms": exec_user_ms,
                    "time_wall_ms": exec_wall_ms,
                    "memory_kb": exec_mem,
                }
                pass_feedback = self._feedback_message_for_pass(pass_feedback_dir, run_root)
                if pass_feedback:
                    p["feedback"] = pass_feedback
                final_pass_row = p
                test_result["verdict"] = "RE"
                test_result["sandbox_status"] = "re"
                break

            checker_verdict = "OK"
            if checker.exists():
                check_result, checker_log, checker_timed_out = self._run_checker(
                    checker,
                    checker_args=checker_args,
                    test=test,
                    team_output=out,
                    answer=ans,
                    pass_feedback_dir=pass_feedback_dir,
                    run_timeout_sec=run_timeout_sec,
                    run_memory_mb=run_memory_mb,
                    run_process_limit=run_process_limit,
                    run_output_kb=run_output_kb,
                )
                (pass_feedback_dir / "checker.log").write_text(checker_log, encoding="utf-8")
                if checker_timed_out:
                    checker_verdict = "FL"
                else:
                    checker_verdict = self._validator_style_verdict(int(check_result.returncode or 0))
            else:
                checker_verdict = "OK" if self._files_equal(ans, out) else "WA"

            p = {
                "verdict": checker_verdict,
                "time_ms": exec_user_ms,
                "time_user_ms": exec_user_ms,
                "time_wall_ms": exec_wall_ms,
                "memory_kb": exec_mem,
            }
            pass_feedback = self._feedback_message_for_pass(pass_feedback_dir, run_root)
            if pass_feedback:
                p["feedback"] = pass_feedback
            final_pass_row = p

            if mode != "multi-pass":
                test_result["verdict"] = checker_verdict
                break

            next_pass = pass_feedback_dir / "nextpass.in"
            if checker_verdict != "OK" or not next_pass.exists() or pass_idx >= max_passes:
                test_result["verdict"] = checker_verdict
                break

            current_input = next_pass
            pass_idx += 1

        if final_pass_row is not None:
            test_result["passes"] = [final_pass_row]
        if str(test_result.get("verdict") or "").strip().upper().startswith("TL"):
            total_user_ms = self._cap_tle_time_ms(total_user_ms, run_timeout_ms)
        test_result["time_ms"] = total_user_ms
        test_result["time_user_ms"] = total_user_ms
        test_result["time_wall_ms"] = total_wall_ms
        test_result["memory_kb"] = peak_mem
        test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
        return test_result

    def _run_multi_pass_interactive_test(
        self,
        sub_bin: Path,
        interactor: Path,
        checker: Path,
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
            "time_user_ms": 0,
            "time_wall_ms": 0,
            "memory_kb": 0,
            "feedback_files": [],
        }
        current_input = test
        pass_idx = 1
        total_user_ms = 0
        total_wall_ms = 0
        peak_mem = 0
        final_pass_row: dict[str, object] | None = None
        while True:
            pass_feedback_dir = test_feedback_dir
            pass_feedback_dir.mkdir(parents=True, exist_ok=True)
            interactor_output = run_root / f"{test.stem}.out"
            transcript = run_root / f"{test.stem}.transcript.txt"
            verdict, exec_user_ms, exec_wall_ms, exec_mem = self._run_interactive_case(
                interactor,
                sub_bin,
                current_input,
                ans,
                interactor_output,
                transcript,
                pass_feedback_dir,
                timeout_sec=run_timeout_sec,
                timeout_ms=run_timeout_ms,
                memory_mb=run_memory_mb,
                process_limit=run_process_limit,
                output_kb=run_output_kb,
            )
            total_user_ms += exec_user_ms
            total_wall_ms += exec_wall_ms
            peak_mem = max(peak_mem, exec_mem)
            interactive_feedback = self._feedback_message_for_pass(pass_feedback_dir / "interactor", run_root)
            if not interactive_feedback:
                interactive_feedback = self._feedback_message_for_pass(pass_feedback_dir, run_root)
            if verdict != "OK":
                p = {
                    "verdict": verdict,
                    "time_ms": exec_user_ms,
                    "time_user_ms": exec_user_ms,
                    "time_wall_ms": exec_wall_ms,
                    "memory_kb": exec_mem,
                }
                if interactive_feedback:
                    p["feedback"] = interactive_feedback
                final_pass_row = p
                test_result["verdict"] = verdict
                if verdict == "TL":
                    test_result["sandbox_status"] = "tle"
                elif verdict == "RE":
                    test_result["sandbox_status"] = "re"
                elif verdict == "FL":
                    test_result["sandbox_status"] = "fail"
                break

            checker_verdict = "OK"
            if checker.exists():
                check_result, checker_log, checker_timed_out = self._run_checker(
                    checker,
                    checker_args=checker_args,
                    test=test,
                    team_output=interactor_output,
                    answer=ans,
                    pass_feedback_dir=pass_feedback_dir,
                    run_timeout_sec=run_timeout_sec,
                    run_memory_mb=run_memory_mb,
                    run_process_limit=run_process_limit,
                    run_output_kb=run_output_kb,
                )
                (pass_feedback_dir / "checker.log").write_text(checker_log, encoding="utf-8")
                if checker_timed_out:
                    checker_verdict = "FL"
                else:
                    checker_verdict = self._validator_style_verdict(int(check_result.returncode or 0))
            else:
                checker_verdict = "OK" if self._files_equal(ans, interactor_output) else "WA"

            p = {
                "verdict": checker_verdict,
                "time_ms": exec_user_ms,
                "time_user_ms": exec_user_ms,
                "time_wall_ms": exec_wall_ms,
                "memory_kb": exec_mem,
            }
            pass_feedback = self._feedback_message_for_pass(pass_feedback_dir, run_root)
            if not pass_feedback:
                pass_feedback = interactive_feedback
            if pass_feedback:
                p["feedback"] = pass_feedback
            final_pass_row = p
            if checker_verdict == "OK":
                test_result["verdict"] = "OK"
                break
            if pass_idx >= max_passes:
                test_result["verdict"] = checker_verdict
                break
            has_next_input = False
            try:
                has_next_input = interactor_output.exists() and interactor_output.is_file() and interactor_output.stat().st_size > 0
            except OSError:
                has_next_input = False
            if not has_next_input:
                test_result["verdict"] = checker_verdict
                break
            current_input = interactor_output
            pass_idx += 1

        if final_pass_row is not None:
            test_result["passes"] = [final_pass_row]
        if str(test_result.get("verdict") or "").strip().upper().startswith("TL"):
            total_user_ms = self._cap_tle_time_ms(total_user_ms, run_timeout_ms)
        test_result["time_ms"] = total_user_ms
        test_result["time_user_ms"] = total_user_ms
        test_result["time_wall_ms"] = total_wall_ms
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
        force_recompile: bool = False,
    ) -> str:
        _ = bool(force_recompile)
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
        safe_expected_behavior = normalize_expected_behavior(str(expected_behavior or "unknown"))
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
        build_ref = ""
        build_row = self.db.fetch_one(
            "SELECT problem_id,workspace_id,status,summary_json,build_ref FROM builds WHERE id=?",
            [build_id],
        )
        preflight_reasons: list[str] = []
        if mode not in supported_modes:
            preflight_reasons.append(f"unsupported run mode: {mode}")
        if build_row is None:
            preflight_reasons.append("build metadata missing")
        else:
            build_ref = str(build_row["build_ref"] or "").strip().lower()
            if not build_ref:
                preflight_reasons.append("build_ref missing")
            else:
                try:
                    artifact_root = self._canonical_build_artifact_root(build_ref)
                except RuntimeError as exc:
                    preflight_reasons.append(str(exc))
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
        run_root = self.fs_manager.prepare_run_root(run_id)
        compile_log_file = run_root / "compile.log"

        self.db.execute(
            "INSERT INTO runs(id,problem_id,workspace_id,build_id,build_ref,mode,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                run_id,
                ctx["problem"]["id"],
                ctx["workspace"]["id"],
                build_id,
                build_ref,
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
            failed_test, failed_reason = self._build_failure_context(build_row)
            if not failed_reason:
                failed_reason = error
            failed_tests = self._synthesize_failure_tests(
                preferred_test=failed_test,
                selected_test_names=selected_test_names,
                reason=failed_reason,
            )
            summary = {
                "error": error,
                "mode": mode,
                "source": submission_path or "upload",
                "tests": failed_tests,
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
        run_cfg = self._load_run_config(artifact_root, default_mode=mode)
        checker_args = [str(x) for x in run_cfg["checker_args"]]
        max_passes = int(run_cfg["max_passes"])
        run_timeout_ms = int(run_cfg.get("run_timeout_ms") or 0)
        run_timeout_sec = int(run_cfg["run_timeout_sec"])
        run_time_limit_ms = int(run_cfg.get("time_limit_ms") or self.DEFAULT_TIME_LIMIT_MS)
        run_memory_mb = int(run_cfg["run_memory_mb"])
        run_process_limit = int(run_cfg["run_process_limit"])
        run_output_kb = int(run_cfg["run_output_kb"])
        progress_persist_min_interval_ms = max(0, int(self.RUN_PROGRESS_PERSIST_MIN_INTERVAL_MS))
        progress_persist_batch_updates = max(1, int(self.RUN_PROGRESS_PERSIST_BATCH_UPDATES))
        progress_pending_updates = 0
        progress_last_persist_monotonic: float | None = None
        progress_has_persisted = False
        sandbox_limits = {
            "cpu_ms": run_time_limit_ms,
            "wall_ms": run_timeout_ms,
            "memory_mb": run_memory_mb,
            "pids": run_process_limit,
            "output_kb": run_output_kb,
        }

        def _persist_running_summary(current_tests: list[dict]) -> None:
            nonlocal progress_pending_updates
            nonlocal progress_last_persist_monotonic
            nonlocal progress_has_persisted
            tests_snapshot = [dict(row) for row in current_tests if isinstance(row, dict)]
            if not tests_snapshot:
                return
            progress_pending_updates += 1
            now_monotonic = time.monotonic()
            should_persist = False
            if not progress_has_persisted:
                # Persist the first progress snapshot eagerly so the UI can show
                # testcase-level activity as soon as it starts.
                should_persist = True
            elif progress_pending_updates >= progress_persist_batch_updates:
                should_persist = True
            elif progress_persist_min_interval_ms <= 0:
                should_persist = True
            elif progress_last_persist_monotonic is None:
                should_persist = True
            else:
                elapsed_ms = int((now_monotonic - progress_last_persist_monotonic) * 1000)
                if elapsed_ms >= progress_persist_min_interval_ms:
                    should_persist = True
            if not should_persist:
                return
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
                    "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in tests_snapshot if isinstance(t, dict)),
                    "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in tests_snapshot if isinstance(t, dict)),
                    "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in tests_snapshot if isinstance(t, dict)] or [0]),
                },
            }
            _attach_invocation_block(summary, completed=False)
            self.db.execute("UPDATE runs SET summary_json=? WHERE id=?", [self._summary_for_db(summary), run_id])
            progress_pending_updates = 0
            progress_last_persist_monotonic = now_monotonic
            progress_has_persisted = True

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
            use_multi_pass_interactive = mode == "multi-pass" and interactor.exists()
            if use_multi_pass_interactive:
                # Each multi-pass case may require multiple interactive rounds.
                effective_run_jobs = 1
                run_cfg["run_jobs_effective"] = 1

            if mode == "interactive":
                for idx, (test_name, test_stem) in enumerate(test_meta):
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
                        "time_user_ms": 0,
                        "time_wall_ms": 0,
                        "memory_kb": 0,
                        "feedback_files": [],
                    }
                    if not interactor.exists():
                        raise RuntimeError("interactive mode requested but interactor is missing in build artifacts")
                    transcript = run_root / f"{test_stem}.transcript.txt"
                    interactor_output = run_root / f"{test_stem}.out"
                    verdict, user_ms, wall_ms, mem_kb = self._run_interactive_case(
                        interactor,
                        sub_bin,
                        test,
                        ans,
                        interactor_output,
                        transcript,
                        test_feedback_dir,
                        timeout_sec=run_timeout_sec,
                        timeout_ms=run_timeout_ms,
                        memory_mb=run_memory_mb,
                        process_limit=run_process_limit,
                        output_kb=run_output_kb,
                    )
                    if str(verdict or "").strip().upper().startswith("TL"):
                        user_ms = self._cap_tle_time_ms(user_ms, run_timeout_ms)
                    test_result["passes"].append(
                        {
                            "verdict": verdict,
                            "time_ms": user_ms,
                            "time_user_ms": user_ms,
                            "time_wall_ms": wall_ms,
                            "memory_kb": mem_kb,
                        }
                    )
                    interactive_feedback = self._feedback_message_for_pass(test_feedback_dir / "interactor", run_root)
                    if not interactive_feedback:
                        interactive_feedback = self._feedback_message_for_pass(test_feedback_dir, run_root)
                    if interactive_feedback and test_result["passes"]:
                        test_result["passes"][0]["feedback"] = interactive_feedback
                    test_result["verdict"] = verdict
                    if str(verdict or "").strip().upper().startswith("TL"):
                        test_result["sandbox_status"] = "tle"
                    elif verdict == "RE":
                        test_result["sandbox_status"] = "re"
                    test_result["time_ms"] = user_ms
                    test_result["time_user_ms"] = user_ms
                    test_result["time_wall_ms"] = wall_ms
                    test_result["memory_kb"] = mem_kb
                    test_result["feedback_files"] = self._feedback_key_files(test_feedback_dir, run_root)
                    test_result["transcript"] = str(transcript.relative_to(run_root))
                    verdicts.append(test_result)
                    if self._is_fl_verdict(test_result.get("verdict")):
                        self._append_fl_skip_tail_tests(
                            verdicts=verdicts,
                            test_meta=test_meta,
                            failed_index=idx,
                            caused_by_test=test_name,
                        )
                        _persist_running_summary(verdicts)
                        break
                    _persist_running_summary(verdicts)
            elif use_multi_pass_interactive:
                for idx, (test_name, test_stem) in enumerate(test_meta):
                    test = tests_dir / test_name
                    ans_name = f"{test_stem}.ans"
                    if ans_name not in answer_names:
                        raise RuntimeError(f"answer file missing or invalid for {test_name}")
                    ans = ans_dir / ans_name
                    test_result = self._run_multi_pass_interactive_test(
                        sub_bin,
                        interactor,
                        checker,
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
                    verdicts.append(test_result)
                    if self._is_fl_verdict(test_result.get("verdict")):
                        self._append_fl_skip_tail_tests(
                            verdicts=verdicts,
                            test_meta=test_meta,
                            failed_index=idx,
                            caused_by_test=test_name,
                        )
                        _persist_running_summary(verdicts)
                        break
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
                    first_fl_index = -1
                    first_fl_test = ""
                    for idx, row in enumerate(parallel_verdicts):
                        if not isinstance(row, dict):
                            continue
                        if not self._is_fl_verdict(row.get("verdict")):
                            continue
                        first_fl_index = idx
                        first_fl_test = str(row.get("test") or test_meta[idx][0])
                        break
                    if first_fl_index >= 0:
                        if not first_fl_test:
                            first_fl_test = test_meta[first_fl_index][0]
                        for rem_idx in range(first_fl_index + 1, len(parallel_verdicts)):
                            rem_test_name = test_meta[rem_idx][0]
                            parallel_verdicts[rem_idx] = self._synthesized_fl_skip_test(
                                rem_test_name,
                                caused_by_test=first_fl_test,
                            )
                    verdicts.extend(parallel_verdicts)
            else:
                for idx, (test_name, test_stem) in enumerate(test_meta):
                    test = tests_dir / test_name
                    ans_name = f"{test_stem}.ans"
                    if ans_name not in answer_names:
                        raise RuntimeError(f"answer file missing or invalid for {test_name}")
                    ans = ans_dir / ans_name
                    test_result = self._run_noninteractive_test(
                        mode,
                        sub_bin,
                        checker,
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
                    verdicts.append(test_result)
                    if self._is_fl_verdict(test_result.get("verdict")):
                        self._append_fl_skip_tail_tests(
                            verdicts=verdicts,
                            test_meta=test_meta,
                            failed_index=idx,
                            caused_by_test=test_name,
                        )
                        _persist_running_summary(verdicts)
                        break
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
                    "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in verdicts if isinstance(t, dict)),
                    "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
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
                    "time_user_ms_total": sum(int(t.get("time_user_ms", t.get("time_ms", 0)) or 0) for t in verdicts if isinstance(t, dict)),
                    "time_wall_ms_total": sum(int(t.get("time_wall_ms", 0) or 0) for t in verdicts if isinstance(t, dict)),
                    "memory_kb_peak": max([int(t.get("memory_kb", 0) or 0) for t in verdicts if isinstance(t, dict)] or [0]),
                },
            }
            _attach_invocation_block(summary, completed=True)
            (run_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._finalize_run(run_id, "failed", summary)

        return run_id
