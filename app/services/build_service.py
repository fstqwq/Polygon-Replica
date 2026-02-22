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
from app.services.toolchain_service import ToolchainService
from app.services.util import run_cmd
from app.services.workspace_service import WorkspaceService


DIAG_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<level>warning|error|note):\s*(?P<msg>.*)$")


class BuildService:
    def __init__(self, db: DB, workspace_service: WorkspaceService, artifacts: ArtifactService, toolchain: ToolchainService):
        self.db = db
        self.workspace_service = workspace_service
        self.artifacts = artifacts
        self.toolchain = toolchain

    def _find_cpp(self, root: Path, folder: str, preferred: str | None = None) -> Path | None:
        base = root / folder
        if not base.exists():
            return None
        if preferred:
            exact = base / preferred
            if exact.exists():
                return exact
            stem = Path(preferred).stem
            for ext in [".cpp", ".cc", ".cxx", ".c++"]:
                candidate = base / f"{stem}{ext}"
                if candidate.exists():
                    return candidate
        files: list[Path] = []
        for pat in ["*.cpp", "*.cc", "*.cxx", "*.c++"]:
            files.extend(sorted(base.glob(pat)))
        return sorted(files)[0] if files else None

    def _resolve_source(self, snapshot: Path, rel_path: str) -> Path:
        p = (snapshot / rel_path).resolve()
        if snapshot.resolve() not in p.parents:
            raise RuntimeError(f"invalid configured source path: {rel_path}")
        if not p.exists() or not p.is_file():
            raise RuntimeError(f"configured source does not exist: {rel_path}")
        return p

    def _select_source(
        self,
        snapshot: Path,
        build_cfg: dict,
        config_key: str,
        folder: str,
        preferred: str | None = None,
    ) -> Path | None:
        configured = build_cfg.get(config_key)
        if configured:
            return self._resolve_source(snapshot, str(configured))
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

    def _collect_diagnostics(self, snapshot: Path, text: str) -> list[dict]:
        result: list[dict] = []
        for line in text.splitlines():
            m = DIAG_RE.match(line.strip())
            if not m:
                continue
            file_path = Path(m.group("file"))
            if file_path.is_absolute():
                try:
                    rel = str(file_path.resolve().relative_to(snapshot.resolve()))
                except ValueError:
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

    def _validator_ok(self, returncode: int) -> bool:
        return returncode in {0, 42}

    def _manual_test_sources(self, snapshot: Path) -> list[Path]:
        manual_root = snapshot / "tests" / "manual"
        if not manual_root.exists():
            return []
        in_files: list[Path] = []
        for p in manual_root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() == ".in":
                in_files.append(p)
        if in_files:
            return sorted(in_files, key=lambda p: str(p.relative_to(manual_root)))

        # Backward-compatible fallback: when no *.in exists, treat all files as manual tests.
        files = [p for p in manual_root.rglob("*") if p.is_file()]
        return sorted(files, key=lambda p: str(p.relative_to(manual_root)))

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
            include_dirs = [snapshot / "third_party/testlib"]
            generator_targets: list[tuple[str, Path | None, Path]] = []
            configured_generators = [str(x) for x in build_cfg.get("generator_sources", []) if str(x).strip()]
            if configured_generators:
                for idx, rel in enumerate(configured_generators, start=1):
                    generator_targets.append((f"generator_{idx}", self._resolve_source(snapshot, rel), bin_dir / f"generator_{idx}"))
            else:
                gen_src = self._select_source(snapshot, build_cfg, "generator_source", "generators")
                generator_targets.append(("generator", gen_src, bin_dir / "generator"))

            accepted_src: Path | None
            if build_cfg.get("accepted_solution_source"):
                accepted_src = self._resolve_source(snapshot, str(build_cfg["accepted_solution_source"]))
            elif build_cfg.get("accepted_source"):
                accepted_src = self._resolve_source(snapshot, str(build_cfg["accepted_source"]))
            else:
                accepted_src = self._find_cpp(snapshot, "solutions", preferred="accepted.cpp") or self._find_cpp(snapshot, "solutions")

            compile_targets = [
                *generator_targets,
                ("validator", self._select_source(snapshot, build_cfg, "validator_source", "validators"), bin_dir / "validator"),
                ("checker", self._select_source(snapshot, build_cfg, "checker_source", "checkers"), bin_dir / "checker"),
                ("interactor", self._select_source(snapshot, build_cfg, "interactor_source", "interactors"), bin_dir / "interactor"),
                ("accepted_solution", accepted_src, bin_dir / "accepted_solution"),
            ]

            compile_plan = [(name, source, output) for name, source, output in compile_targets if source is not None]
            compile_jobs = self._effective_compile_jobs(build_cfg.get("compile_jobs", 0), len(compile_plan))
            compile_results: dict[str, tuple[bool, str, str, str]] = {}
            if compile_plan:
                with ThreadPoolExecutor(max_workers=compile_jobs) as pool:
                    future_map = {
                        pool.submit(self.toolchain.compile_cpp, source, output, include_dirs, [snapshot]): name
                        for name, source, output in compile_plan
                    }
                    for future in as_completed(future_map):
                        name = future_map[future]
                        compile_results[name] = future.result()

            compiled_bins: dict[str, Path] = {}
            compile_log = [f"compile_jobs={compile_jobs}"]
            for name, source, output in compile_targets:
                if source is None:
                    compile_log.append(f"[{name}] missing source\n")
                    continue
                ok, out, err, toolchain_digest = compile_results[name]
                merged = f"{out}\n{err}".strip()
                diagnostics.extend(self._collect_diagnostics(snapshot, merged))
                compile_log.append(f"[{name}] source={source}\n{merged}\n")
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

            (logs_dir / "compile.log").write_text("\n".join(compile_log), encoding="utf-8")
            steps.append({"step": "compile", "status": "ok", "log": "logs/compile.log"})

            current_step = "generate"
            tests = self._manual_test_sources(snapshot)
            test_files: list[Path] = []
            counter = 1
            for t in tests:
                dst = artifact_paths.tests / f"{counter:03d}.in"
                shutil.copy2(t, dst)
                test_files.append(dst)
                counter += 1

            generator_bins = [compiled_bins[name] for name, _, _ in generator_targets if name in compiled_bins]
            gen_logs: list[str] = []
            generated_count = 0
            if generator_bins:
                runs = int(build_cfg.get("generator_runs", 3))
                generator_args = [str(x) for x in build_cfg.get("generator_args", [])]
                for gen_index, gen in enumerate(generator_bins, start=1):
                    for i in range(runs):
                        dst = artifact_paths.tests / f"{counter:03d}.in"
                        proc = run_cmd([str(gen), *generator_args], stdout_path=dst, timeout=30)
                        gen_logs.append(
                            f"generator={gen_index} case={i + 1} rc={proc.returncode}\n{proc.stderr}\n"
                        )
                        if proc.returncode != 0:
                            dst.unlink(missing_ok=True)
                            failing_test = dst.name
                            raise RuntimeError(f"generator failed on generator={gen_index} case={i + 1}")
                        test_files.append(dst)
                        generated_count += 1
                        counter += 1
            if not test_files:
                raise RuntimeError("no tests were generated (manual + generator)")
            (logs_dir / "generate.log").write_text(
                (
                    f"manual_tests={len(tests)}\n"
                    f"generated_tests={generated_count}\n"
                    f"total_tests={len(test_files)}\n"
                    + "\n".join(gen_logs)
                ),
                encoding="utf-8",
            )
            steps.append({"step": "generate", "status": "ok", "log": "logs/generate.log"})

            current_step = "validate"
            validator = compiled_bins["validator"]
            validator_args = [str(x) for x in build_cfg.get("validator_args", [])]
            validate_jobs = self._effective_compile_jobs(build_cfg.get("validate_jobs", 0), len(test_files))
            validate_results: dict[str, tuple[int, str | None]] = {}
            validate_root = logs_dir / "validate_runs"
            validate_root.mkdir(parents=True, exist_ok=True)
            with (logs_dir / "validate.log").open("w", encoding="utf-8") as vlog:
                vlog.write(f"validate_jobs={validate_jobs}\n")
                with ThreadPoolExecutor(max_workers=validate_jobs) as pool:
                    future_map = {}
                    for t in test_files:
                        test_cwd = validate_root / t.stem
                        test_cwd.mkdir(parents=True, exist_ok=True)
                        future_map[
                            pool.submit(
                                run_cmd,
                                [str(validator), *validator_args],
                                stdin_path=t,
                                timeout=30,
                                cwd=test_cwd,
                            )
                        ] = t
                    for future in as_completed(future_map):
                        t = future_map[future]
                        try:
                            proc = future.result()
                            validate_results[t.name] = (proc.returncode, None)
                            vlog.write(f"{t.name}: args={validator_args} rc={proc.returncode}\n{proc.stdout}{proc.stderr}\n")
                        except Exception as exc:
                            validate_results[t.name] = (-1, str(exc))
                            vlog.write(f"{t.name}: args={validator_args} rc=-1\n{exc}\n")

                for t in test_files:
                    failing_test = t.name
                    rc, err = validate_results[t.name]
                    if err is not None:
                        raise RuntimeError(f"validator failed on {t.name}: {err}")
                    if not self._validator_ok(rc):
                        raise RuntimeError(f"validator failed on {t.name}")
            steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

            current_step = "solve"
            accepted = compiled_bins["accepted_solution"]
            solve_jobs = self._effective_compile_jobs(build_cfg.get("solve_jobs", 0), len(test_files))
            solve_results: dict[str, tuple[int, str | None]] = {}
            with (logs_dir / "solve.log").open("w", encoding="utf-8") as slog:
                slog.write(f"solve_jobs={solve_jobs}\n")
                with ThreadPoolExecutor(max_workers=solve_jobs) as pool:
                    future_map = {}
                    for t in test_files:
                        out = artifact_paths.ans / t.name.replace(".in", ".ans")
                        future_map[pool.submit(run_cmd, [str(accepted)], stdin_path=t, stdout_path=out, timeout=30)] = t
                    for future in as_completed(future_map):
                        t = future_map[future]
                        try:
                            proc = future.result()
                            solve_results[t.name] = (proc.returncode, None)
                            slog.write(f"{t.name}: rc={proc.returncode}\n{proc.stderr}\n")
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
            self.artifacts.write_manifest(
                artifact_paths,
                source_commit=source_commit,
                source_ref=source_ref,
                toolchain_digest=toolchain_digest,
                seed=seed,
                generation_params={
                    "generator_runs": int(build_cfg.get("generator_runs", 3)),
                    "compile_jobs": compile_jobs,
                    "validate_jobs": int(build_cfg.get("validate_jobs", 0)),
                    "validate_jobs_effective": validate_jobs,
                    "solve_jobs": int(build_cfg.get("solve_jobs", 0)),
                    "solve_jobs_effective": solve_jobs,
                    "run_jobs": int(build_cfg.get("run_jobs", 0)),
                    "run_timeout_sec": int(build_cfg.get("run_timeout_sec", 30)),
                    "generator_sources": [str(x) for x in build_cfg.get("generator_sources", [])],
                    "generator_args": [str(x) for x in build_cfg.get("generator_args", [])],
                    "validator_args": [str(x) for x in build_cfg.get("validator_args", [])],
                    "checker_args": [str(x) for x in build_cfg.get("checker_args", [])],
                    "checker_mode": str(build_cfg.get("checker_mode", "testlib")),
                    "max_passes": int(build_cfg.get("max_passes", 16)),
                },
                steps=steps,
            )

            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                ["ok", json.dumps({"steps": steps, "diagnostics": diagnostics}), now_iso(), build_id],
            )
            final_status = "ok"
        except Exception as exc:
            (logs_dir / "failure.log").write_text(str(exc), encoding="utf-8")
            steps.append({"step": current_step, "status": "error", "log": "logs/failure.log"})
            self.db.execute(
                "UPDATE builds SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [
                    "failed",
                    json.dumps(
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
