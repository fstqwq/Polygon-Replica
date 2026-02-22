from __future__ import annotations

import json
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
        if preferred and (base / preferred).exists():
            return base / preferred
        files = sorted(base.glob("*.cpp"))
        return files[0] if files else None

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

    def run_build(self, problem: str, username: str, commit: str | None = None, ref: str | None = None) -> str:
        build_id = f"b-{uuid.uuid4().hex[:12]}"
        ctx = self.workspace_service.workspace_context(problem, username)
        workspace = Path(ctx["workspace"]["path"])

        with self.workspace_service.workspace_lock(workspace):
            snapshot = self.workspace_service.create_snapshot(workspace, commit)

        artifact_paths = self.artifacts.prepare(problem, build_id)
        logs_dir = artifact_paths.logs
        bin_dir = artifact_paths.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        source_commit = commit or run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
        source_ref = ref or ctx["workspace"].get("branch") or "main"

        ws_row = self.db.fetch_one("SELECT id FROM workspaces WHERE problem_id=? AND user_id=?", [ctx["problem"]["id"], ctx["user"]["id"]])
        self.db.execute(
            "INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [build_id, ctx["problem"]["id"], ws_row["id"], source_commit, source_ref, "running", str(artifact_paths.root), now_iso()],
        )

        steps: list[dict] = []
        toolchain_digest = "unknown"
        seed = random.randint(1, 10**9)
        random.seed(seed)
        diagnostics: list[dict] = []
        build_cfg = self._load_build_config(snapshot)
        current_step = "compile"
        failing_test: str | None = None

        try:
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

            compiled_bins: dict[str, Path] = {}
            compile_log = []
            for name, source, output in compile_targets:
                if source is None:
                    compile_log.append(f"[{name}] missing source\n")
                    continue
                ok, out, err, toolchain_digest = self.toolchain.compile_cpp(source, output, include_dirs)
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
            tests = sorted((snapshot / "tests/manual").glob("*")) if (snapshot / "tests/manual").exists() else []
            test_files: list[Path] = []
            counter = 1
            for t in tests:
                if t.is_file():
                    dst = artifact_paths.tests / f"{counter:03d}.in"
                    shutil.copy2(t, dst)
                    test_files.append(dst)
                    counter += 1

            generator_bins = [compiled_bins[name] for name, _, _ in generator_targets if name in compiled_bins]
            gen_logs: list[str] = []
            if generator_bins:
                runs = int(build_cfg.get("generator_runs", 3))
                generator_args = [str(x) for x in build_cfg.get("generator_args", [])]
                for gen_index, gen in enumerate(generator_bins, start=1):
                    for i in range(runs):
                        dst = artifact_paths.tests / f"{counter:03d}.in"
                        proc = run_cmd([str(gen), *generator_args], timeout=30)
                        gen_logs.append(
                            f"generator={gen_index} case={i + 1} rc={proc.returncode}\n{proc.stderr}\n"
                        )
                        if proc.returncode != 0:
                            failing_test = dst.name
                            raise RuntimeError(f"generator failed on generator={gen_index} case={i + 1}")
                        dst.write_text(proc.stdout, encoding="utf-8")
                        test_files.append(dst)
                        counter += 1
            if not test_files:
                raise RuntimeError("no tests were generated (manual + generator)")
            (logs_dir / "generate.log").write_text(
                f"generated_tests={len(test_files)}\n" + "\n".join(gen_logs),
                encoding="utf-8",
            )
            steps.append({"step": "generate", "status": "ok", "log": "logs/generate.log"})

            current_step = "validate"
            validator = compiled_bins["validator"]
            validator_args = [str(x) for x in build_cfg.get("validator_args", [])]
            vlogs = []
            for t in test_files:
                failing_test = t.name
                proc = run_cmd([str(validator), *validator_args], stdin_path=t, timeout=30)
                vlogs.append(f"{t.name}: args={validator_args} rc={proc.returncode}\n{proc.stdout}{proc.stderr}\n")
                if not self._validator_ok(proc.returncode):
                    raise RuntimeError(f"validator failed on {t.name}")
            (logs_dir / "validate.log").write_text("\n".join(vlogs), encoding="utf-8")
            steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

            current_step = "solve"
            accepted = compiled_bins["accepted_solution"]
            slog = []
            for t in test_files:
                failing_test = t.name
                out = artifact_paths.ans / t.name.replace(".in", ".ans")
                proc = run_cmd([str(accepted)], stdin_path=t, stdout_path=out, timeout=30)
                slog.append(f"{t.name}: rc={proc.returncode}\n{proc.stderr}\n")
                if proc.returncode != 0:
                    raise RuntimeError(f"accepted solution failed on {t.name}")
            (logs_dir / "solve.log").write_text("\n".join(slog), encoding="utf-8")
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
        finally:
            self.db.execute(
                "UPDATE workspaces SET recent_build_status=? WHERE id=?",
                [self.db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id])["status"], ws_row["id"]],
            )
            shutil.rmtree(snapshot.parent, ignore_errors=True)

        return build_id
