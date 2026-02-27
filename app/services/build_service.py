from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import random
import re
import shutil
import uuid
from pathlib import Path

from app.db import DB, now_iso
from app.services.artifact_service import ArtifactService
from app.services.sandbox import ExecSpec, SandboxBackend, create_sandbox_backend
from app.services.solution_metadata import (
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
    parse_solution_desc,
)
from app.services.tests_spec import (
    load_tests_spec,
    payload_rel_path_for_test,
    parse_gen_command_tokens,
)
from app.services.toolchain_service import ToolchainService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")
CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[2] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
DEFAULT_TIME_LIMIT_MS = 2000
TIME_LIMIT_MIN_MS = 100
TIME_LIMIT_MAX_MS = 30000


class BuildService:
    DB_SUMMARY_DIAGNOSTICS_LIMIT = 200
    DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT = 4096

    def __init__(
        self,
        db: DB,
        workspace_service: WorkspaceService,
        artifacts: ArtifactService,
        toolchain: ToolchainService,
        sandbox_backend: SandboxBackend | None = None,
    ):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.toolchain = toolchain
        self.sandbox = sandbox_backend or create_sandbox_backend()
        self.default_exec_memory_mb = self._env_int("POLYGONLIKE_BUILD_MEMORY_MB", default=1024, min_value=16, max_value=262144)
        self.default_exec_process_limit = self._env_int("POLYGONLIKE_BUILD_PROCESS_LIMIT", default=64, min_value=1, max_value=4096)
        self.default_exec_output_kb = self._env_int("POLYGONLIKE_BUILD_OUTPUT_KB", default=65536, min_value=64, max_value=1048576)

    def _env_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def _sandbox_exec(
        self,
        cmd: list[str],
        timeout_sec: int,
        *,
        cwd: Path | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str, bool]:
        result = self.sandbox.run(
            ExecSpec(
                command=cmd,
                cwd=cwd,
                timeout_sec=max(1, int(timeout_sec)),
                stdin_path=stdin_path,
                stdout_path=stdout_path,
                env=env,
                memory_mb=self.default_exec_memory_mb,
                process_limit=self.default_exec_process_limit,
                output_kb=self.default_exec_output_kb,
            )
        )
        if result.timed_out:
            return -1, result.stdout, result.stderr, True
        return int(result.returncode or 0), result.stdout, result.stderr, False

    def _cap_summary_list_field(
        self,
        payload: dict,
        field: str,
        limit: int,
        truncated_key: str,
        total_key: str,
        limit_key: str,
    ) -> None:
        values = payload.get(field)
        if not isinstance(values, list):
            return
        cap = max(1, int(limit))
        total = len(values)
        payload[limit_key] = cap
        payload[total_key] = total
        if total > cap:
            payload[field] = values[:cap]
            payload[truncated_key] = True
            return
        payload[truncated_key] = False

    def _summary_for_db(self, summary: dict) -> str:
        payload = dict(summary)
        self._cap_summary_list_field(
            payload,
            "diagnostics",
            self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            "diagnostics_truncated",
            "diagnostics_total",
            "diagnostics_limit",
        )
        diagnostics = payload.get("diagnostics")
        if isinstance(diagnostics, list):
            payload["diagnostics"] = self._normalize_diagnostics_for_db(
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

    def _compact_single_line(self, value: str, max_chars: int) -> str:
        text = " ".join(str(value or "").split())
        cap = max(1, int(max_chars))
        if len(text) <= cap:
            return text
        return text[:cap].rstrip() + "..."

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
            normalized.append(row)
        return normalized

    def _is_safe_source_in_dir(self, root: Path, path: Path, root_resolved: Path | None = None) -> bool:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return False
        try:
            resolved_root = root_resolved if root_resolved is not None else root.resolve()
            resolved = path.resolve()
        except OSError:
            return False
        return resolved_root in resolved.parents or resolved_root == resolved

    def _find_cpp(self, root: Path, folder: str, preferred: str | None = None) -> Path | None:
        return self._find_source_with_extensions(root, folder, CPP_EXTENSIONS, preferred=preferred)

    def _find_source_with_extensions(
        self,
        root: Path,
        folder: str,
        extensions: tuple[str, ...],
        preferred: str | None = None,
    ) -> Path | None:
        base = root / folder
        if not base.exists() or not base.is_dir():
            return None
        try:
            base_resolved = base.resolve()
        except OSError:
            return None
        if preferred:
            exact = base / preferred
            if self._is_safe_source_in_dir(base, exact, root_resolved=base_resolved):
                return exact
            stem = Path(preferred).stem
            for ext in extensions:
                candidate = base / f"{stem}{ext}"
                if self._is_safe_source_in_dir(base, candidate, root_resolved=base_resolved):
                    return candidate
        try:
            best: Path | None = None
            best_name = ""
            with os.scandir(base) as entries:
                for entry in entries:
                    name = entry.name
                    if Path(name).suffix.lower() not in extensions:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if best is None or name < best_name:
                        best = base / name
                        best_name = name
        except OSError:
            return None
        return best

    def _find_solution_by_expected_behavior(self, root: Path, expected_behavior: str) -> Path | None:
        base = root / "solutions"
        if not base.exists() or not base.is_dir():
            return None
        expected = normalize_expected_behavior(expected_behavior)
        if expected == "unknown":
            return None
        try:
            base_resolved = base.resolve()
        except OSError:
            return None
        matches: list[tuple[str, Path]] = []
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    name = str(entry.name or "")
                    if Path(name).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    source_path = base / name
                    behavior = infer_expected_behavior_from_name(f"solutions/{name}")
                    desc_path = base / f"{name}.desc"
                    if self._is_safe_source_in_dir(base, desc_path, root_resolved=base_resolved):
                        try:
                            desc_text = desc_path.read_text(encoding="utf-8", errors="replace")
                            parsed = parse_solution_desc(desc_text)
                            behavior = normalize_expected_behavior(str(parsed.get("expected_behavior") or ""))
                        except OSError:
                            pass
                    if behavior == expected:
                        matches.append((name, source_path))
        except OSError:
            return None
        if not matches:
            return None
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    def _resolve_source(self, snapshot: Path, rel_path: str, snapshot_resolved: Path | None = None) -> Path:
        resolved_snapshot = snapshot_resolved if snapshot_resolved is not None else snapshot.resolve()
        p = (snapshot / rel_path).resolve()
        if resolved_snapshot not in p.parents:
            raise RuntimeError(f"invalid configured source path: {rel_path}")
        if not p.exists() or not p.is_file():
            raise RuntimeError(f"configured source does not exist: {rel_path}")
        return p

    def _normalize_standard_checker_name(self, raw: str) -> str:
        value = str(raw or "").strip()
        if value.startswith("std::"):
            value = value[5:]
        if not value:
            raise RuntimeError("checker_standard is empty")
        if "/" in value or "\\" in value:
            raise RuntimeError("checker_standard is invalid")
        if not value.endswith(".cpp"):
            value += ".cpp"
        if not STANDARD_CHECKER_NAME_RE.fullmatch(value):
            raise RuntimeError("checker_standard is invalid")
        return value

    def _resolve_standard_checker_source(self, checker_standard: str) -> Path | None:
        raw = str(checker_standard or "").strip()
        if not raw:
            return None
        checker_name = self._normalize_standard_checker_name(raw)
        source = (STANDARD_CHECKER_ROOT / checker_name).resolve()
        try:
            source.relative_to(STANDARD_CHECKER_ROOT)
        except ValueError:
            raise RuntimeError("checker_standard is invalid")
        try:
            if source.is_symlink() or not source.exists() or not source.is_file():
                raise RuntimeError(f"configured standard checker does not exist: std::{checker_name}")
        except OSError:
            raise RuntimeError("standard checker catalog is unavailable")
        return source

    def _select_checker_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        standard_source = self._resolve_standard_checker_source(str(build_cfg.get("checker_standard") or ""))
        if standard_source is not None:
            return standard_source
        return self._select_source(
            snapshot,
            build_cfg,
            "checker_source",
            "checkers",
            snapshot_resolved=snapshot_resolved,
        )

    def _select_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        config_key: str,
        folder: str,
        preferred: str | None = None,
        snapshot_resolved: Path | None = None,
    ) -> Path | None:
        configured = build_cfg.get(config_key)
        if configured:
            return self._resolve_source(snapshot, str(configured), snapshot_resolved=snapshot_resolved)
        return self._find_cpp(snapshot, folder, preferred=preferred)

    def _load_build_config(self, snapshot: Path) -> dict:
        cfg = {
            "generator_runs": 3,
            "require_generator": False,
            "require_validator": True,
            "require_checker": True,
            "compile_jobs": 0,
            "validate_jobs": 0,
            "solve_jobs": 0,
            "run_jobs": 0,
            "run_timeout_sec": 30,
            "generator_args": [],
            "generator_sources": [],
            "validator_args": [],
            "checker_args": [],
            "checker_mode": "testlib",
            "checker_standard": "",
            "max_passes": 16,
        }
        path = snapshot / "config" / "build.json"
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        if not isinstance(cfg.get("generator_args"), list):
            cfg["generator_args"] = []
        if not isinstance(cfg.get("generator_sources"), list):
            cfg["generator_sources"] = []
        if not isinstance(cfg.get("validator_args"), list):
            cfg["validator_args"] = []
        if not isinstance(cfg.get("checker_args"), list):
            cfg["checker_args"] = []
        if not isinstance(cfg.get("checker_standard"), str):
            cfg["checker_standard"] = ""
        cfg["checker_standard"] = str(cfg.get("checker_standard") or "").strip()
        try:
            cfg["compile_jobs"] = max(0, min(16, int(cfg.get("compile_jobs", 0))))
        except Exception:
            cfg["compile_jobs"] = 0
        try:
            cfg["validate_jobs"] = max(0, min(16, int(cfg.get("validate_jobs", 0))))
        except Exception:
            cfg["validate_jobs"] = 0
        try:
            cfg["solve_jobs"] = max(0, min(16, int(cfg.get("solve_jobs", 0))))
        except Exception:
            cfg["solve_jobs"] = 0
        try:
            cfg["run_jobs"] = max(0, min(16, int(cfg.get("run_jobs", 0))))
        except Exception:
            cfg["run_jobs"] = 0
        try:
            cfg["run_timeout_sec"] = max(1, min(300, int(cfg.get("run_timeout_sec", 30))))
        except Exception:
            cfg["run_timeout_sec"] = 30
        cfg["checker_mode"] = str(cfg.get("checker_mode", "testlib")).lower()
        if cfg["checker_mode"] not in {"testlib", "kattis"}:
            cfg["checker_mode"] = "testlib"
        try:
            cfg["max_passes"] = max(1, int(cfg.get("max_passes", 16)))
        except Exception:
            cfg["max_passes"] = 16
        return cfg

    def _normalize_time_limit_ms(self, raw: object) -> int:
        try:
            value = int(raw)
        except Exception:
            value = DEFAULT_TIME_LIMIT_MS
        return max(TIME_LIMIT_MIN_MS, min(TIME_LIMIT_MAX_MS, value))

    def _effective_run_timeout_ms(self, time_limit_ms: int) -> int:
        tl = self._normalize_time_limit_ms(time_limit_ms)
        return max(tl * 2, tl + 1000)

    def _effective_run_timeout_sec(self, run_timeout_ms: int) -> int:
        timeout_ms = max(1, int(run_timeout_ms))
        return max(1, (timeout_ms + 999) // 1000)

    def _load_problem_runtime_config(self, snapshot: Path) -> dict:
        cfg = {"time_limit_ms": DEFAULT_TIME_LIMIT_MS}
        path = snapshot / "config" / "problem.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    cfg.update(payload)
            except json.JSONDecodeError:
                pass
        cfg["time_limit_ms"] = self._normalize_time_limit_ms(cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
        return cfg

    def _collect_diagnostics(self, snapshot: Path, text: str) -> list[dict]:
        result: list[dict] = []
        try:
            snapshot_resolved = snapshot.resolve()
        except OSError:
            snapshot_resolved = None
        for line in text.splitlines():
            m = DIAG_RE.match(line.strip())
            if not m:
                continue
            file_path = Path(m.group("file"))
            if file_path.is_absolute():
                try:
                    resolved = file_path.resolve()
                    if snapshot_resolved is not None:
                        rel = str(resolved.relative_to(snapshot_resolved))
                    else:
                        rel = str(resolved)
                except ValueError:
                    rel = str(file_path)
                except OSError:
                    rel = str(file_path)
            else:
                rel = str(file_path)
            result.append(
                {
                    "file": rel,
                    "line": int(m.group("line")),
                    "column": int(m.group("col")),
                    "level": m.group("level"),
                    "message": m.group("msg"),
                }
            )
        return result

    def _append_compile_streams(
        self,
        log_fh,
        snapshot: Path,
        stdout_text: str,
        stderr_text: str,
    ) -> list[dict]:
        diagnostics: list[dict] = []
        saw_stream_text = False
        wrote_stream = False
        for chunk in (stdout_text, stderr_text):
            text = str(chunk or "")
            if not text:
                continue
            saw_stream_text = True
            if wrote_stream and not text.startswith("\n"):
                log_fh.write("\n")
            log_fh.write(text)
            if not text.endswith("\n"):
                log_fh.write("\n")
            diagnostics.extend(self._collect_diagnostics(snapshot, text))
            wrote_stream = True
        if not saw_stream_text:
            diagnostics.extend(self._collect_diagnostics(snapshot, ""))
        return diagnostics

    def _validator_ok(self, returncode: int) -> bool:
        return returncode in {0, 42}

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        manual_root = snapshot / "tests" / "manual"
        if not manual_root.exists():
            return []
        try:
            manual_root_resolved = manual_root.resolve()
        except OSError:
            return []

        def _is_in_name(name: str) -> bool:
            return os.path.splitext(name)[1].lower() == ".in"

        def _collect_safe_entries(
            dir_root: Path,
            names: list[str],
            rel_prefix: str,
        ) -> list[tuple[str, Path, bool]]:
            safe_entries: list[tuple[str, Path, bool]] = []
            for name in names:
                p = dir_root / name
                if p.is_symlink() or not p.exists() or not p.is_file():
                    continue
                rel = f"{rel_prefix}/{name}" if rel_prefix else name
                safe_entries.append((rel, p, _is_in_name(name)))
            return safe_entries

        in_files: list[tuple[str, Path]] = []
        all_files: list[tuple[str, Path]] | None = []
        for dirpath, dirnames, filenames in os.walk(manual_root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if manual_root_resolved not in dir_root_resolved.parents and manual_root_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            try:
                rel_root = dir_root.relative_to(manual_root)
            except ValueError:
                dirnames[:] = []
                continue
            rel_prefix = "" if rel_root == Path(".") else rel_root.as_posix()
            keep_dirs: list[str] = []
            for name in dirnames:
                d = dir_root / name
                if d.is_symlink():
                    continue
                keep_dirs.append(name)
            dirnames[:] = sorted(keep_dirs)

            in_candidates = [name for name in filenames if _is_in_name(name)]
            has_in_file = False
            if in_candidates:
                # Fast path: when safe *.in files exist, we can skip validating sidecar files.
                safe_entries = _collect_safe_entries(dir_root, in_candidates, rel_prefix)
                has_in_file = bool(safe_entries)
                if not has_in_file and all_files is not None:
                    safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)
                    has_in_file = any(is_in for _, _, is_in in safe_entries)
            elif all_files is None:
                safe_entries = []
            else:
                safe_entries = _collect_safe_entries(dir_root, filenames, rel_prefix)

            if has_in_file:
                if all_files is not None:
                    all_files.clear()
                    all_files = None
                for rel, p, is_in in safe_entries:
                    if is_in:
                        in_files.append((rel, p))
            elif all_files is not None:
                for rel, p, _ in safe_entries:
                    all_files.append((rel, p))

        if in_files:
            return [p for _, p in sorted(in_files)]
        return []

    def _load_tests_spec(self, snapshot: Path) -> list[dict] | None:
        spec_path = snapshot / "tests" / "spec.json"
        if not spec_path.exists():
            return None
        try:
            return load_tests_spec(spec_path)
        except ValueError as exc:
            raise RuntimeError(f"invalid tests/spec.json: {exc}") from exc

    def _generator_source_catalog(self, snapshot: Path) -> list[tuple[str, Path]]:
        generators_root = snapshot / "generators"
        try:
            if not generators_root.exists() or not generators_root.is_dir() or generators_root.is_symlink():
                return []
        except OSError:
            return []
        try:
            generators_root_resolved = generators_root.resolve()
        except OSError:
            return []

        rows: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(generators_root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if (
                generators_root_resolved not in dir_root_resolved.parents
                and generators_root_resolved != dir_root_resolved
            ):
                dirnames[:] = []
                continue

            safe_dirs: list[str] = []
            for name in dirnames:
                p = dir_root / name
                try:
                    if p.is_symlink() or not p.exists() or not p.is_dir():
                        continue
                except OSError:
                    continue
                safe_dirs.append(name)
            dirnames[:] = sorted(safe_dirs)

            for name in sorted(filenames):
                if Path(name).suffix.lower() not in CPP_EXTENSIONS:
                    continue
                p = dir_root / name
                try:
                    if p.is_symlink() or not p.exists() or not p.is_file():
                        continue
                    rel = str(p.relative_to(snapshot)).replace("\\", "/")
                except (OSError, ValueError):
                    continue
                rows.append((rel, p))
        rows.sort(key=lambda item: item[0])
        return rows

    def _resolve_generator_source_from_token(
        self,
        token: str,
        generator_catalog: list[tuple[str, Path]],
    ) -> tuple[str, Path]:
        raw = str(token or "").strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw:
            raise RuntimeError("generator command is empty")
        if any(part == ".." for part in raw.split("/")):
            raise RuntimeError(f"invalid generator command '{token}'")

        by_rel = {rel: path for rel, path in generator_catalog}
        candidates: list[str] = []
        token_path = Path(raw)
        suffix = token_path.suffix.lower()
        if raw.startswith("generators/"):
            if suffix in CPP_EXTENSIONS:
                candidates.append(raw)
            else:
                for ext in CPP_EXTENSIONS:
                    candidates.append(f"{raw}{ext}")
        else:
            if suffix in CPP_EXTENSIONS:
                candidates.append(f"generators/{raw}")
            else:
                candidates.append(f"generators/{raw}")
                for ext in CPP_EXTENSIONS:
                    candidates.append(f"generators/{raw}{ext}")

        seen: set[str] = set()
        for rel in candidates:
            rel_key = str(rel or "").strip()
            if not rel_key or rel_key in seen:
                continue
            seen.add(rel_key)
            hit = by_rel.get(rel_key)
            if hit is not None:
                return rel_key, hit

        name = token_path.name
        if suffix in CPP_EXTENSIONS:
            exact = [(rel, p) for rel, p in generator_catalog if Path(rel).name == name]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise RuntimeError(f"ambiguous generator source for command '{token}'")
        else:
            stem = token_path.name
            stem_matches = [(rel, p) for rel, p in generator_catalog if Path(rel).stem == stem]
            if len(stem_matches) == 1:
                return stem_matches[0]
            if len(stem_matches) > 1:
                raise RuntimeError(f"ambiguous generator source for command '{token}'")

        raise RuntimeError(f"cannot resolve generator source for command '{token}'")

    def _tests_spec_payload_text(self, snapshot: Path, row: dict, index: int) -> tuple[str, str]:
        test_id = str(row.get("id") or "").strip()
        if not test_id:
            raise RuntimeError(f"tests/spec.json entry {index} missing id")
        kind = str(row.get("kind") or "").strip().lower()
        if kind not in {"manual", "gen"}:
            raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
        rel = payload_rel_path_for_test(test_id, kind)
        payload_path = snapshot / rel
        try:
            if payload_path.exists() and payload_path.is_file() and not payload_path.is_symlink():
                return rel, payload_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read tests payload for id {test_id}: {exc}") from exc
        raise RuntimeError(f"missing tests payload file for id {test_id}: {rel}")

    def _prepare_tests_spec_runtime(
        self,
        snapshot: Path,
        tests_spec_entries: list[dict],
        bin_dir: Path,
    ) -> tuple[list[dict], list[tuple[str, Path, Path]]]:
        runtime_entries: list[dict] = []
        generator_targets: list[tuple[str, Path, Path]] = []
        by_source_rel: dict[str, tuple[str, Path]] = {}
        generator_catalog = self._generator_source_catalog(snapshot)

        for index, row in enumerate(tests_spec_entries, start=1):
            kind = str(row.get("kind") or "").strip()
            test_id = str(row.get("id") or "").strip()
            sample = bool(row.get("sample"))
            payload_rel, payload = self._tests_spec_payload_text(snapshot, row, index)
            if kind == "manual":
                runtime_entries.append(
                    {
                        "index": index,
                        "id": test_id,
                        "kind": "manual",
                        "sample": sample,
                        "source_rel": payload_rel,
                        "input": payload,
                    }
                )
                continue
            if kind != "gen":
                raise RuntimeError(f"invalid test kind at tests/spec.json entry {index}")
            command = str(payload or "").strip()
            tokens = parse_gen_command_tokens(command)
            source_rel, source_path = self._resolve_generator_source_from_token(tokens[0], generator_catalog)
            compiled = by_source_rel.get(source_rel)
            if compiled is None:
                gen_index = len(by_source_rel) + 1
                target_name = f"generator_spec_{gen_index}"
                target_bin = bin_dir / target_name
                by_source_rel[source_rel] = (target_name, target_bin)
                generator_targets.append((target_name, source_path, target_bin))
                compiled = (target_name, target_bin)
            runtime_entries.append(
                {
                    "index": index,
                    "id": test_id,
                    "kind": "gen",
                    "sample": sample,
                    "cmd": command,
                    "args": [str(x) for x in tokens[1:]],
                    "source_rel": source_rel,
                    "payload_rel": payload_rel,
                    "target_name": compiled[0],
                }
            )

        return runtime_entries, generator_targets

    def _effective_compile_jobs(self, configured: object, target_count: int) -> int:
        auto_jobs = max(1, min(4, os.cpu_count() or 1))
        try:
            requested = int(configured)
        except Exception:
            requested = 0
        bounded = auto_jobs if requested <= 0 else max(1, min(16, requested))
        return max(1, min(bounded, max(1, target_count)))

    def run_build(self, problem: str, username: str, commit: str | None = None, ref: str | None = None) -> str:
        build_id = f"b-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])
        workspace_id = int(ctx["workspace"]["id"])
        source_commit = "" if commit else str(ctx["workspace"].get("head_commit") or "").strip()
        source_ref = ref or commit or ctx["workspace"].get("branch") or "main"
        artifact_paths = self.artifacts.prepare(problem, build_id)
        logs_dir = artifact_paths.logs
        bin_dir = artifact_paths.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.db.execute(
            "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [build_id, ctx["problem"]["id"], workspace_id, source_commit, source_ref, "running", str(artifact_paths.root), now_iso()],
        )

        steps: list[dict] = []
        toolchain_digest = "unknown"
        seed = random.randint(1, 10**9)
        random.seed(seed)
        diagnostics: list[dict] = []
        build_cfg: dict = {}
        tests_spec_entries: list[dict] | None = None
        tests_spec_runtime: list[dict] = []
        current_step = "compile"
        failing_test: str | None = None
        snapshot: Path | None = None
        final_status = "running"

        try:
            if commit:
                source_commit = self.workspace_service.resolve_commit(workspace, commit)
                source_ref = ref or commit
                self.db.execute("UPDATE builds SET source_commit=?, source_ref=? WHERE id=?", [source_commit, source_ref, build_id])
                snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
            else:
                with self.workspace_service.workspace_lock(workspace):
                    status = self.workspace_service.read_workspace_status(workspace)
                    source_commit = str(status.get("head_commit") or "").strip()
                    branch = str(status.get("branch") or "").strip()
                    dirty = bool(status.get("dirty"))
                    if not source_commit:
                        source_commit = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                    if branch:
                        source_ref = ref or branch
                    self.db.execute("UPDATE builds SET source_commit=?, source_ref=? WHERE id=?", [source_commit, source_ref, build_id])
                    snapshot = self.workspace_service.create_snapshot(
                        workspace,
                        None,
                        workspace_head=source_commit,
                        workspace_dirty=dirty,
                    )

            build_cfg = self._load_build_config(snapshot)
            runtime_cfg = self._load_problem_runtime_config(snapshot)
            time_limit_ms = int(runtime_cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
            run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms)
            run_timeout_sec = self._effective_run_timeout_sec(run_timeout_ms)
            try:
                snapshot_resolved = snapshot.resolve()
            except OSError:
                snapshot_resolved = None
            include_dirs = [snapshot / "third_party/testlib"]
            generator_targets: list[tuple[str, Path | None, Path]] = []
            tests_spec_entries = self._load_tests_spec(snapshot)
            if tests_spec_entries is not None:
                tests_spec_runtime, tests_spec_generators = self._prepare_tests_spec_runtime(
                    snapshot,
                    tests_spec_entries,
                    bin_dir,
                )
                generator_targets.extend(tests_spec_generators)
            else:
                configured_generators = [str(x) for x in build_cfg.get("generator_sources", []) if str(x).strip()]
                if configured_generators:
                    for idx, rel in enumerate(configured_generators, start=1):
                        generator_targets.append(
                            (
                                f"generator_{idx}",
                                self._resolve_source(snapshot, rel, snapshot_resolved=snapshot_resolved),
                                bin_dir / f"generator_{idx}",
                            )
                        )
                else:
                    generator_targets.append(("generator", None, bin_dir / "generator"))

            accepted_rel = str(build_cfg.get("accepted_solution_source") or "").strip()
            if not accepted_rel:
                raise RuntimeError("accepted solution source is required")
            if not accepted_rel.startswith("solutions/"):
                raise RuntimeError("accepted solution source must be under solutions/")
            if Path(accepted_rel).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                raise RuntimeError("accepted solution source must be .cpp/.cc/.cxx/.c++/.py/.java")
            accepted_src = self._resolve_source(
                snapshot,
                accepted_rel,
                snapshot_resolved=snapshot_resolved,
            )

            compile_targets = [
                *generator_targets,
                (
                    "validator",
                    self._select_source(
                        snapshot,
                        build_cfg,
                        "validator_source",
                        "validators",
                        snapshot_resolved=snapshot_resolved,
                    ),
                    bin_dir / "validator",
                ),
                (
                    "checker",
                    self._select_checker_source(snapshot, build_cfg, snapshot_resolved=snapshot_resolved),
                    bin_dir / "checker",
                ),
                (
                    "interactor",
                    self._select_source(
                        snapshot,
                        build_cfg,
                        "interactor_source",
                        "interactors",
                        snapshot_resolved=snapshot_resolved,
                    ),
                    bin_dir / "interactor",
                ),
                ("accepted_solution", accepted_src, bin_dir / "accepted_solution"),
            ]

            compile_plan = [(name, source, output) for name, source, output in compile_targets if source is not None]
            compile_plan_cpp = [(name, source, output) for name, source, output in compile_plan if name != "accepted_solution"]
            compile_jobs = self._effective_compile_jobs(build_cfg.get("compile_jobs", 0), len(compile_plan_cpp))
            compile_results: dict[str, tuple[bool, str, str, str]] = {}
            if compile_plan_cpp:
                with ThreadPoolExecutor(max_workers=compile_jobs) as pool:
                    future_map = {
                        pool.submit(self.toolchain.compile_cpp, source, output, include_dirs, [snapshot]): name
                        for name, source, output in compile_plan_cpp
                    }
                    for future in as_completed(future_map):
                        name = future_map[future]
                        compile_results[name] = future.result()
            accepted_target = next((row for row in compile_targets if row[0] == "accepted_solution"), None)
            if accepted_target is not None:
                _accepted_name, accepted_source, accepted_output = accepted_target
                if accepted_source is not None:
                    compile_results["accepted_solution"] = self.toolchain.compile_program(
                        accepted_source,
                        accepted_output,
                        include_dirs,
                        path_roots=[snapshot],
                    )

            compiled_bins: dict[str, Path] = {}
            compile_log_path = logs_dir / "compile.log"
            with compile_log_path.open("w", encoding="utf-8") as clog:
                clog.write(f"compile_jobs={compile_jobs}\n")
                for name, source, output in compile_targets:
                    if source is None:
                        clog.write(f"[{name}] missing source\n\n")
                        continue
                    ok, out, err, toolchain_digest = compile_results[name]
                    clog.write(f"[{name}] source={source}\n")
                    diagnostics.extend(
                        self._append_compile_streams(
                            clog,
                            snapshot,
                            out,
                            err,
                        )
                    )
                    clog.write("\n")
                    if not ok:
                        raise RuntimeError(f"compile failed: {name}")
                    compiled_bins[name] = output

            has_generator_compiled = any(name.startswith("generator") for name in compiled_bins)
            if build_cfg.get("require_generator") and not has_generator_compiled:
                raise RuntimeError("generator is required by config/build.json but missing")
            if build_cfg.get("require_validator", True) and "validator" not in compiled_bins:
                raise RuntimeError("validator source is required")
            if build_cfg.get("require_checker", True) and "checker" not in compiled_bins:
                raise RuntimeError("checker source is required")
            if "accepted_solution" not in compiled_bins:
                raise RuntimeError("accepted solution source is required")

            steps.append({"step": "compile", "status": "ok", "log": "logs/compile.log"})

            current_step = "generate"
            test_files: list[Path] = []
            tests_meta: list[dict] = []
            counter = 1
            manual_count = 0
            generated_count = 0
            generate_log_path = logs_dir / "generate.log"
            with generate_log_path.open("w", encoding="utf-8") as glog:
                if tests_spec_entries is not None:
                    glog.write("tests_source=tests/spec.json\n")
                    for row in tests_spec_runtime:
                        kind = str(row.get("kind") or "")
                        test_id = str(row.get("id") or "").strip()
                        is_sample = bool(row.get("sample"))
                        dst = artifact_paths.tests / f"{counter:03d}.in"
                        if kind == "manual":
                            input_text = str(row.get("input") or "")
                            dst.write_text(input_text, encoding="utf-8")
                            test_files.append(dst)
                            manual_count += 1
                            tests_meta.append(
                                {
                                    "index": counter,
                                    "kind": "manual",
                                    "id": test_id,
                                    "sample": is_sample,
                                    "desc": f"manual {test_id}" if test_id else "manual",
                                    "source": str(row.get("source_rel") or "tests/spec.json"),
                                }
                            )
                            glog.write(f"manual id={test_id} index={row.get('index')} -> {dst.name}\n")
                            counter += 1
                            continue

                        if kind != "gen":
                            raise RuntimeError(f"invalid test kind at tests/spec.json entry {row.get('index')}")
                        target_name = str(row.get("target_name") or "")
                        gen_bin = compiled_bins.get(target_name)
                        if gen_bin is None:
                            raise RuntimeError(
                                f"generator source is required for tests/spec.json entry {row.get('index')}"
                            )
                        args = [str(x) for x in row.get("args") or []]
                        rc, _out, err, timed_out = self._sandbox_exec(
                            [str(gen_bin), *args],
                            timeout_sec=30,
                            stdout_path=dst,
                        )
                        glog.write(
                            f"gen id={test_id} index={row.get('index')} source={row.get('source_rel')} cmd={row.get('cmd')} rc={rc}\n{err}\n"
                        )
                        if timed_out or rc != 0:
                            dst.unlink(missing_ok=True)
                            failing_test = dst.name
                            raise RuntimeError(
                                f"generator failed on tests/spec.json entry {row.get('index')} (id={test_id})"
                            )
                        test_files.append(dst)
                        generated_count += 1
                        desc = str(row.get("cmd") or "").strip() or "gen"
                        tests_meta.append(
                            {
                                "index": counter,
                                "kind": "gen",
                                "id": test_id,
                                "sample": is_sample,
                                "desc": desc,
                                "command": str(row.get("cmd") or "").strip(),
                                "source": str(row.get("source_rel") or "").strip(),
                                "payload_source": str(row.get("payload_rel") or "").strip(),
                            }
                        )
                        counter += 1
                else:
                    tests = self._manual_test_sources(snapshot)
                    for t in tests:
                        dst = artifact_paths.tests / f"{counter:03d}.in"
                        shutil.copy2(t, dst)
                        test_files.append(dst)
                        manual_count += 1
                        try:
                            source_rel = str(t.relative_to(snapshot)).replace("\\", "/")
                        except ValueError:
                            source_rel = str(t.name)
                        tests_meta.append(
                            {
                                "index": counter,
                                "kind": "manual",
                                "desc": f"manual: {source_rel}",
                                "source": source_rel,
                            }
                        )
                        counter += 1

                    generator_execs: list[tuple[int, str, Path]] = []
                    for gen_index, (name, source, _target) in enumerate(generator_targets, start=1):
                        gen_bin = compiled_bins.get(name)
                        if gen_bin is None:
                            continue
                        if source is None:
                            source_label = f"generator:{name}"
                        else:
                            try:
                                source_label = str(source.relative_to(snapshot)).replace("\\", "/")
                            except ValueError:
                                source_label = str(source)
                        generator_execs.append((gen_index, source_label, gen_bin))

                    if generator_execs:
                        runs = int(build_cfg.get("generator_runs", 3))
                        generator_args = [str(x) for x in build_cfg.get("generator_args", [])]
                        for gen_index, source_label, gen in generator_execs:
                            for i in range(runs):
                                dst = artifact_paths.tests / f"{counter:03d}.in"
                                rc, _out, err, timed_out = self._sandbox_exec(
                                    [str(gen), *generator_args],
                                    timeout_sec=30,
                                    stdout_path=dst,
                                )
                                glog.write(
                                    f"generator={gen_index} source={source_label} case={i + 1} rc={rc}\n{err}\n"
                                )
                                if timed_out or rc != 0:
                                    dst.unlink(missing_ok=True)
                                    failing_test = dst.name
                                    raise RuntimeError(f"generator failed on generator={gen_index} case={i + 1}")
                                test_files.append(dst)
                                generated_count += 1
                                desc = f"gen: {source_label}"
                                if generator_args:
                                    desc = f"{desc} {' '.join(generator_args)}"
                                tests_meta.append(
                                    {
                                        "index": counter,
                                        "kind": "gen",
                                        "desc": desc,
                                        "source": source_label,
                                    }
                                )
                                counter += 1
                glog.write(f"manual_tests={manual_count}\n")
                glog.write(f"generated_tests={generated_count}\n")
                glog.write(f"total_tests={len(test_files)}\n")
            if not test_files:
                if tests_spec_entries is not None:
                    raise RuntimeError("no tests were generated from tests/spec.json")
                raise RuntimeError("no tests were generated (manual + generator)")
            (logs_dir / "tests_meta.json").write_text(json.dumps(tests_meta, indent=2), encoding="utf-8")
            steps.append({"step": "generate", "status": "ok", "log": "logs/generate.log"})

            current_step = "validate"
            validator = compiled_bins["validator"]
            validator_args = [str(x) for x in build_cfg.get("validator_args", [])]
            validate_jobs = self._effective_compile_jobs(build_cfg.get("validate_jobs", 0), len(test_files))
            validate_results: dict[str, dict[str, object]] = {}
            validate_root = logs_dir / "validate_runs"
            validate_root.mkdir(parents=True, exist_ok=True)
            with (logs_dir / "validate.log").open("w", encoding="utf-8") as vlog:
                vlog.write(f"validate_jobs={validate_jobs}\n")

                def _validate_case(test_path: Path, test_cwd: Path) -> tuple[int, str, str, bool]:
                    return self._sandbox_exec(
                        [str(validator), *validator_args],
                        timeout_sec=30,
                        stdin_path=test_path,
                        cwd=test_cwd,
                    )

                with ThreadPoolExecutor(max_workers=validate_jobs) as pool:
                    future_map = {}
                    for t in test_files:
                        test_cwd = validate_root / t.stem
                        test_cwd.mkdir(parents=True, exist_ok=True)
                        future_map[pool.submit(_validate_case, t, test_cwd)] = t
                    for future in as_completed(future_map):
                        t = future_map[future]
                        try:
                            rc, out, err, timed_out = future.result()
                            validate_results[t.name] = {
                                "rc": int(rc),
                                "worker_error": "",
                                "timed_out": bool(timed_out),
                                "stderr": str(err or ""),
                            }
                            timeout_note = " timed_out=1" if timed_out else ""
                            vlog.write(f"{t.name}: args={validator_args} rc={rc}{timeout_note}\n{out}{err}\n")
                        except Exception as exc:
                            validate_results[t.name] = {
                                "rc": -1,
                                "worker_error": str(exc),
                                "timed_out": False,
                                "stderr": "",
                            }
                            vlog.write(f"{t.name}: args={validator_args} rc=-1\n{exc}\n")

                for t in test_files:
                    failing_test = t.name
                    row = validate_results[t.name]
                    rc = int(row.get("rc") or 0)
                    worker_error = str(row.get("worker_error") or "").strip()
                    timed_out = bool(row.get("timed_out"))
                    stderr_text = self._compact_single_line(str(row.get("stderr") or ""), 220)
                    if worker_error:
                        raise RuntimeError(f"validator failed on {t.name}: {worker_error}")
                    if not self._validator_ok(rc):
                        if timed_out:
                            base_msg = f"validator failed on {t.name} (rc={rc}, timed_out=1)"
                        else:
                            base_msg = f"validator failed on {t.name} (rc={rc})"
                        if stderr_text:
                            raise RuntimeError(f"{base_msg}: stderr: {stderr_text}")
                        raise RuntimeError(base_msg)
            steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

            current_step = "solve"
            accepted = compiled_bins["accepted_solution"]
            solve_jobs = self._effective_compile_jobs(build_cfg.get("solve_jobs", 0), len(test_files))
            solve_results: dict[str, tuple[int, str | None]] = {}
            with (logs_dir / "solve.log").open("w", encoding="utf-8") as slog:
                slog.write(f"solve_jobs={solve_jobs}\n")

                def _solve_case(test_path: Path, out_path: Path) -> tuple[int, str, str, bool]:
                    return self._sandbox_exec(
                        [str(accepted)],
                        timeout_sec=30,
                        stdin_path=test_path,
                        stdout_path=out_path,
                    )

                with ThreadPoolExecutor(max_workers=solve_jobs) as pool:
                    future_map = {}
                    for t in test_files:
                        out = artifact_paths.ans / t.name.replace(".in", ".ans")
                        future_map[pool.submit(_solve_case, t, out)] = t
                    for future in as_completed(future_map):
                        t = future_map[future]
                        try:
                            rc, _out, err, timed_out = future.result()
                            solve_results[t.name] = (rc, None)
                            timeout_note = " timed_out=1" if timed_out else ""
                            slog.write(f"{t.name}: rc={rc}{timeout_note}\n{err}\n")
                        except Exception as exc:
                            solve_results[t.name] = (-1, str(exc))
                            slog.write(f"{t.name}: rc=-1\n{exc}\n")

                for t in test_files:
                    failing_test = t.name
                    rc, err = solve_results[t.name]
                    if err is not None:
                        raise RuntimeError(f"accepted solution failed on {t.name}: {err}")
                    if rc != 0:
                        raise RuntimeError(f"accepted solution failed on {t.name}")
            steps.append({"step": "solve", "status": "ok", "log": "logs/solve.log"})

            (logs_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
            generation_params = {
                "tests_spec_enabled": tests_spec_entries is not None,
                "tests_spec_entries": len(tests_spec_runtime) if tests_spec_entries is not None else 0,
                "generator_runs": int(build_cfg.get("generator_runs", 3)),
                "compile_jobs": compile_jobs,
                "validate_jobs": int(build_cfg.get("validate_jobs", 0)),
                "validate_jobs_effective": validate_jobs,
                "solve_jobs": int(build_cfg.get("solve_jobs", 0)),
                "solve_jobs_effective": solve_jobs,
                "run_jobs": int(build_cfg.get("run_jobs", 0)),
                "time_limit_ms": time_limit_ms,
                "run_timeout_ms": run_timeout_ms,
                "run_timeout_sec": run_timeout_sec,
                "generator_sources": [str(x) for x in build_cfg.get("generator_sources", [])],
                "generator_args": [str(x) for x in build_cfg.get("generator_args", [])],
                "validator_args": [str(x) for x in build_cfg.get("validator_args", [])],
                "checker_args": [str(x) for x in build_cfg.get("checker_args", [])],
                "checker_mode": str(build_cfg.get("checker_mode", "testlib")),
                "checker_standard": str(build_cfg.get("checker_standard", "")),
                "max_passes": int(build_cfg.get("max_passes", 16)),
                "sandbox_backend": self.sandbox.name,
                "sandbox_memory_mb": self.default_exec_memory_mb,
                "sandbox_process_limit": self.default_exec_process_limit,
                "sandbox_output_kb": self.default_exec_output_kb,
            }
            # Small runner-focused config sidecar avoids full manifest reads on run setup hot paths.
            (logs_dir / "run_config.json").write_text(json.dumps(generation_params, indent=2), encoding="utf-8")
            self.artifacts.write_manifest(
                artifact_paths,
                source_commit=source_commit,
                source_ref=source_ref,
                toolchain_digest=toolchain_digest,
                seed=seed,
                generation_params=generation_params,
                steps=steps,
            )

            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["ok", self._summary_for_db({"steps": steps, "diagnostics": diagnostics}), now_iso(), build_id],
            )
            final_status = "ok"
        except Exception as exc:
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
                (logs_dir / "failure.log").write_text(str(exc), encoding="utf-8")
            except Exception:
                pass
            steps.append({"step": current_step, "status": "error", "log": "logs/failure.log"})
            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [
                    "failed",
                    self._summary_for_db(
                        {
                            "error": str(exc),
                            "failed_step": current_step,
                            "failed_test": failing_test,
                            "steps": steps,
                            "diagnostics": diagnostics,
                        }
                    ),
                    now_iso(),
                    build_id,
                ],
            )
            final_status = "failed"
        finally:
            if final_status != "running":
                self.db.execute(
                    "UPDATE workspaces SET recent_build_status=? WHERE id=?",
                    [final_status, workspace_id],
                )
            if snapshot is not None:
                shutil.rmtree(snapshot.parent, ignore_errors=True)

        return build_id
