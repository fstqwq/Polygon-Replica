from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import random
import re
import shlex
import shutil
import uuid

from app.db import now_iso
from app.service.platform.process import run_cmd
from app.service.build.diagnostic import compact_single_line, normalize_diagnostics_for_db
from app.service.build.source import resolve_source
from app.service.build.summary import summary_for_db
from app.service.build.test_spec import tests_spec_answer_source

CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
DEFAULT_TIME_LIMIT_MS = 2000


def run_build(
    self,
    problem: str,
    username: str,
    commit: str | None = None,
    ref: str | None = None,
    *,
    sample_only: bool = False,
    verification_pipeline: bool = False,
) -> str:
    ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
    workspace = Path(ctx["workspace"]["path"])
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    source_commit = ""
    source_ref = ref or commit or ctx["workspace"].get("branch") or "main"
    resolved_commit_override = ""
    generation_params_digest = ""
    toolchain_cmd_digest = self._toolchain_cmd_digest() or "unknown"
    use_build_result_cache = False
    cache_key: dict[str, object] | None = None
    cache_key_hash = ""
    inflight_owner = False
    inflight_snapshot: Path | None = None
    try:
        if commit:
            try:
                resolved_commit_override = self.workspace_service.resolve_commit(workspace, commit)
            except Exception:
                resolved_commit_override = ""
            if resolved_commit_override:
                source_commit = resolved_commit_override
                source_ref = ref or commit
                inflight_snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
                try:
                    generation_params_digest = self._generation_params_digest(
                        inflight_snapshot,
                        sample_only=bool(sample_only),
                    )
                except Exception:
                    generation_params_digest = ""
        else:
            with self.workspace_service.workspace_lock(workspace):
                status = self.workspace_service.read_workspace_status(workspace)
                workspace_dirty = bool(status.get("dirty"))
                workspace_head = str(status.get("head_commit") or "").strip()
                workspace_branch = str(status.get("branch") or "").strip()
                if not workspace_head:
                    workspace_head = run_cmd(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
                if workspace_branch and (not ref):
                    source_ref = workspace_branch
            if workspace_head and (not workspace_dirty):
                source_commit = workspace_head
                try:
                    generation_params_digest = self._generation_params_digest(
                        workspace,
                        sample_only=bool(sample_only),
                    )
                except Exception:
                    generation_params_digest = ""
            elif not workspace_dirty:
                try:
                    generation_params_digest = self._generation_params_digest(
                        workspace,
                        sample_only=bool(sample_only),
                    )
                except Exception:
                    generation_params_digest = ""
                if generation_params_digest:
                    source_commit = f"workspace:{generation_params_digest}"
    finally:
        if inflight_snapshot is not None:
            shutil.rmtree(inflight_snapshot.parent, ignore_errors=True)

    if source_commit and generation_params_digest:
        use_build_result_cache = True
        cache_key = self._build_cache_key(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=str(source_commit or "").strip(),
            source_ref=str(source_ref or "").strip(),
            generation_params_digest=str(generation_params_digest or "").strip().lower(),
            toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
            sample_only=bool(sample_only),
        )
        cached_build_id = self._cached_build_id_for_source(
            problem_slug=problem,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=str(source_commit or "").strip(),
            source_ref=str(source_ref or "").strip(),
            generation_params_digest=str(generation_params_digest or "").strip().lower(),
            toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
            sample_only=bool(sample_only),
        )
        if cached_build_id:
            return cached_build_id
        cache_key_hash = self._build_cache_key_hash(cache_key)

    build_ref_key = (
        cache_key
        if isinstance(cache_key, dict)
        else self._build_cache_key(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=str(source_commit or "").strip(),
            source_ref=str(source_ref or "").strip(),
            generation_params_digest=str(generation_params_digest or "").strip().lower(),
            toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
            sample_only=bool(sample_only),
        )
    )
    build_ref = self._build_ref_from_cache_key_hash(self._build_cache_key_hash(build_ref_key))
    build_id = f"b-{uuid.uuid4().hex[:12]}"
    if cache_key is not None:
        existing_build_id = ""
        with self._build_inflight_lock:
            existing_build_id = str(self._build_inflight.get(cache_key_hash) or "").strip()
            if not existing_build_id:
                self._build_inflight[cache_key_hash] = build_id
                inflight_owner = True
        if existing_build_id:
            status = self._wait_build_terminal_status(existing_build_id, self.BUILD_JOIN_WAIT_TIMEOUT_SEC)
            if status == "ok":
                return existing_build_id
            if status in {"failed", "cancelled"}:
                raise RuntimeError("same-configuration build already failed; check logs and retry")
            raise RuntimeError("same-configuration build is still running; refresh later")

    artifact_paths = self._build_paths(problem, build_ref)
    # Build refs are content-addressed and can be reused across retries. Ensure
    # each build starts from a clean artifact layout to avoid stale files from a
    # previous failed/incomplete attempt leaking into current verification.
    for directory in (
        artifact_paths.tests,
        artifact_paths.ans,
        artifact_paths.logs,
        artifact_paths.bin,
        artifact_paths.export,
        artifact_paths.statement_preview,
    ):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
    logs_dir = artifact_paths.logs
    bin_dir = artifact_paths.root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    self.db.execute(
        "INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,artifact_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [build_id, build_ref, problem_id, workspace_id, source_commit, source_ref, "running", str(artifact_paths.root), now_iso()],
    )

    steps: list[dict] = []
    toolchain_digest = "unknown"
    seed = random.randint(1, 10**9)
    random.seed(seed)
    diagnostics: list[dict] = []
    build_cfg: dict = {}
    tests_spec_entries: list[dict] | None = None
    tests_spec_runtime: list[dict] = []
    custom_sample_rows_by_test: dict[str, dict[str, object]] = {}
    current_step = "compile"
    failing_test: str | None = None
    snapshot: Path | None = None
    final_status = "running"

    try:
        if commit:
            source_commit = resolved_commit_override or self.workspace_service.resolve_commit(workspace, commit)
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
                if (not source_commit) and (not dirty):
                    try:
                        synthetic_digest = self._generation_params_digest(
                            workspace,
                            sample_only=bool(sample_only),
                        )
                    except Exception:
                        synthetic_digest = ""
                    if synthetic_digest:
                        source_commit = f"workspace:{synthetic_digest}"
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
        problem_mode = self._normalize_problem_mode(runtime_cfg.get("mode"), "pass-fail")
        interactive_mode = problem_mode == "interactive"
        time_limit_ms = int(runtime_cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
        run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms, mode=problem_mode)
        run_timeout_sec = self._effective_run_timeout_sec(run_timeout_ms)
        build_solve_timeout_sec = max(1, (max(1, int(time_limit_ms)) + 999) // 1000)
        try:
            snapshot_resolved = snapshot.resolve()
        except OSError:
            snapshot_resolved = None
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
                            resolve_source(snapshot, rel, snapshot_resolved=snapshot_resolved),
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
        accepted_src = resolve_source(
            snapshot,
            accepted_rel,
            snapshot_resolved=snapshot_resolved,
        )

        compile_targets = [
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
        generator_source_by_name: dict[str, Path] = {
            str(name): source
            for name, source, _output in generator_targets
            if isinstance(source, Path)
        }
        compile_source_by_name: dict[str, Path] = {
            str(name): source
            for name, source, _output in compile_targets
            if isinstance(source, Path)
        }

        compile_plan = [(name, source, output) for name, source, output in compile_targets if source is not None]
        compile_jobs = self._effective_compile_jobs(build_cfg.get("compile_jobs", 0), len(compile_plan))
        compile_results: dict[str, tuple[bool, str, str, str]] = {}
        compile_backend = getattr(self, "_judgehost_task_service", None)
        if compile_backend is None:
            raise RuntimeError("judge backend unavailable for build compile")
        try:
            if (not compile_backend.enabled()) or (not compile_backend.auth_token_configured()):
                raise RuntimeError("judge backend unavailable for build compile")
        except Exception as exc:
            raise RuntimeError("judge backend unavailable for build compile") from exc

        def _first_compile_message(summary: dict) -> str:
            diagnostics_obj = summary.get("compile_diagnostics")
            if isinstance(diagnostics_obj, list):
                for item in diagnostics_obj:
                    if not isinstance(item, dict):
                        continue
                    message = str(item.get("message") or "").strip()
                    if message:
                        return message
            return str(summary.get("error") or "").strip()

        def _run_summary_output_ref(summary: dict) -> str:
            tests_obj = summary.get("tests")
            tests = tests_obj if isinstance(tests_obj, list) else []
            for row in tests:
                if not isinstance(row, dict):
                    continue
                passes_obj = row.get("passes")
                passes = passes_obj if isinstance(passes_obj, list) else []
                for pass_row in passes:
                    if not isinstance(pass_row, dict):
                        continue
                    output_ref = str(pass_row.get("output_ref") or pass_row.get("output_artifact") or "").strip()
                    if output_ref:
                        return output_ref
            return ""

        def _run_summary_work_root(summary: dict) -> Path | None:
            judgehost_obj = summary.get("judgehost")
            if not isinstance(judgehost_obj, dict):
                return None
            task_id = str(judgehost_obj.get("task_id") or "").strip()
            if not task_id:
                return None
            try:
                job_row = self.db.fetch_one(
                    "SELECT work_root FROM judgehost_domjudge_jobs WHERE task_id=? ORDER BY job_id DESC LIMIT 1",
                    [task_id],
                )
            except Exception:
                job_row = None
            if job_row is not None:
                work_root = str(job_row["work_root"] or "").strip()
                if work_root:
                    try:
                        return Path(work_root).resolve()
                    except Exception:
                        return None
            resolver = getattr(compile_backend, "_domjudge_work_root", None)
            if not callable(resolver):
                return None
            try:
                return Path(str(resolver(task_id))).resolve()
            except Exception:
                return None

        def _run_summary_verdict(summary: dict) -> str:
            tests_obj = summary.get("tests")
            tests = tests_obj if isinstance(tests_obj, list) else []
            for row in tests:
                if not isinstance(row, dict):
                    continue
                verdict = str(row.get("verdict") or "").strip().upper()
                if verdict:
                    return verdict
                passes_obj = row.get("passes")
                passes = passes_obj if isinstance(passes_obj, list) else []
                for pass_row in passes:
                    if not isinstance(pass_row, dict):
                        continue
                    verdict = str(pass_row.get("verdict") or "").strip().upper()
                    if verdict:
                        return verdict
            return ""

        def _run_summary_feedback_line(summary: dict) -> str:
            tests_obj = summary.get("tests")
            tests = tests_obj if isinstance(tests_obj, list) else []
            for row in tests:
                if not isinstance(row, dict):
                    continue
                passes_obj = row.get("passes")
                passes = passes_obj if isinstance(passes_obj, list) else []
                for pass_row in passes:
                    if not isinstance(pass_row, dict):
                        continue
                    feedback = str(pass_row.get("feedback") or "").strip()
                    if feedback:
                        return feedback
                feedback = str(row.get("feedback") or "").strip()
                if feedback:
                    return feedback
            return ""

        def _run_summary_test_result_map(summary: dict) -> dict[str, dict[str, str]]:
            tests_obj = summary.get("tests")
            tests = tests_obj if isinstance(tests_obj, list) else []
            result_map: dict[str, dict[str, str]] = {}
            for row in tests:
                if not isinstance(row, dict):
                    continue
                test_name = str(row.get("test") or "").strip()
                if not test_name:
                    continue
                passes_obj = row.get("passes")
                pass_rows = [item for item in passes_obj if isinstance(item, dict)] if isinstance(passes_obj, list) else []
                first_pass = pass_rows[0] if pass_rows else {}
                final_pass: dict[str, object] | None = None
                for item in pass_rows:
                    verdict_token = str(item.get("verdict") or "").strip().upper()
                    if verdict_token and verdict_token != "-":
                        final_pass = item
                if final_pass is None:
                    final_pass = first_pass if isinstance(first_pass, dict) else {}
                verdict = str(
                    row.get("verdict")
                    or final_pass.get("verdict")
                    or (first_pass.get("verdict") if isinstance(first_pass, dict) else "")
                    or ""
                ).strip().upper()
                feedback = str(
                    final_pass.get("feedback")
                    or (first_pass.get("feedback") if isinstance(first_pass, dict) else "")
                    or row.get("feedback")
                    or ""
                ).strip()
                output_ref = ""
                for key in ("output_ref", "output_artifact", "output_rel"):
                    token = str(
                        final_pass.get(key)
                        or (first_pass.get(key) if isinstance(first_pass, dict) else "")
                        or row.get(key)
                        or ""
                    ).strip()
                    if token:
                        output_ref = token
                        break
                result_map[test_name] = {
                    "verdict": verdict,
                    "feedback": feedback,
                    "output_ref": output_ref,
                }
            return result_map

        def _run_generator_inputs_via_judgehost(
            *, generator_source: Path, args_list: list[list[str]]
        ) -> list[tuple[int, str]]:
            safe_args_list = [[str(item or "") for item in (args or [])] for args in args_list]
            if not safe_args_list:
                return []
            run_id = f"r-bg-{uuid.uuid4().hex[:12]}"
            invocation_id = f"inv-buildgen-{build_id[:12]}-{uuid.uuid4().hex[:8]}"
            source_bytes = generator_source.read_bytes()
            validator_source = compile_source_by_name.get("validator")
            sources_payload: dict[str, str] = {}
            binaries_payload: dict[str, str] = {}

            testlib_blob: bytes | None = None
            workspace_testlib = (snapshot / "third_party" / "testlib" / "testlib.h").resolve()
            if workspace_testlib.exists() and workspace_testlib.is_file():
                testlib_blob = workspace_testlib.read_bytes()
            else:
                upstream_testlib = (
                    Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "testlib.h"
                ).resolve()
                if upstream_testlib.exists() and upstream_testlib.is_file():
                    testlib_blob = upstream_testlib.read_bytes()

            if isinstance(validator_source, Path) and validator_source.exists() and validator_source.is_file():
                sources_payload["validator.cpp"] = base64.b64encode(validator_source.read_bytes()).decode("ascii")
                if testlib_blob is not None:
                    sources_payload["testlib.h"] = base64.b64encode(testlib_blob).decode("ascii")

            tests_payload: list[dict[str, str]] = []
            for case_index, args in enumerate(safe_args_list, start=1):
                command_payload = " ".join(
                    ['"$SUBMISSION_BIN"', *[shlex.quote(str(item or "")) for item in args]]
                ).strip()
                if not command_payload:
                    command_payload = '"$SUBMISSION_BIN"'
                case_name = f"{case_index:03d}.in"
                tests_payload.append(
                    {
                        "name": case_name,
                        "input_b64": base64.b64encode((command_payload + "\n").encode("utf-8")).decode("ascii"),
                        "answer_name": f"{case_index:03d}.ans",
                        "answer_b64": "",
                    }
                )

            prepared_payload: dict[str, object] = {
                "build_payload": {
                    "tests": tests_payload,
                    "run_config_json": json.dumps(
                        {
                            "checker_mode": "testlib",
                            "checker_args": [],
                            "max_passes": 1,
                            "time_limit_ms": 30000,
                            "memory_limit_mb": int(runtime_cfg.get("memory_limit_mb", 1024)),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "problem_limits": {
                        "time_limit_ms": 30000,
                        "memory_limit_mb": int(runtime_cfg.get("memory_limit_mb", 1024)),
                    },
                    "binaries_b64": binaries_payload,
                    "sources_b64": sources_payload,
                }
            }

            if generator_source.suffix.lower() in CPP_EXTENSIONS and testlib_blob is not None:
                prepared_payload["extra_sources_b64"] = {
                    "testlib.h": base64.b64encode(testlib_blob).decode("ascii")
                }

            task_id = compile_backend.enqueue_task(
                problem=problem,
                username=username,
                build_id=build_id,
                mode=problem_mode,
                submission_path=None,
                upload_content=source_bytes,
                upload_filename=generator_source.name,
                run_id=run_id,
                selected_tests=[],
                invocation_id=invocation_id,
                invocation_run_ids=[run_id],
                expected_behavior="accepted",
                invocation_source="build.generate-input",
                task_kind="generate",
                compile_only=False,
                prepared_payload=prepared_payload,
            )
            waited_run_id = str(compile_backend.wait_for_task(task_id, timeout_sec=None) or run_id).strip() or run_id
            run_row = self.db.fetch_one(
                "SELECT status,summary_json,artifact_path FROM runs WHERE id=?",
                [waited_run_id],
            )
            if run_row is None:
                return [(1, "judge backend generate result missing") for _ in safe_args_list]
            run_status = str(run_row["status"] or "").strip().lower()
            summary_obj: dict = {}
            raw_summary = str(run_row["summary_json"] or "").strip()
            if raw_summary:
                try:
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, dict):
                        summary_obj = parsed
                except Exception:
                    summary_obj = {}
            if run_status and run_status != "ok":
                detail = str(summary_obj.get("error") or "").strip() or f"judge backend run status is {run_status}"
                return [(1, detail) for _ in safe_args_list]
            run_root = Path(str(run_row["artifact_path"] or "")).resolve()
            work_root_hint = _run_summary_work_root(summary_obj)
            test_result_map = _run_summary_test_result_map(summary_obj)
            results: list[tuple[int, str]] = []
            for case_index in range(1, len(safe_args_list) + 1):
                case_name = f"{case_index:03d}.in"
                case_result = test_result_map.get(case_name, {})
                verdict = str(case_result.get("verdict") or "").strip().upper()
                if verdict and verdict != "OK":
                    detail = str(case_result.get("feedback") or "").strip()
                    if (not detail) and verdict == "CE":
                        detail = _first_compile_message(summary_obj)
                    if not detail:
                        detail = str(summary_obj.get("error") or "").strip()
                    if not detail:
                        detail = f"judge verdict {verdict}"
                    results.append((1, detail))
                    continue
                output_ref = str(case_result.get("output_ref") or "").strip()
                output_blob: bytes | None = None
                if output_ref:
                    try:
                        output_blob = compile_backend.resolve_artifact_blob(output_ref, work_root=work_root_hint)
                    except Exception:
                        output_blob = None
                if output_blob is None:
                    fallback = (run_root / f"{case_index:03d}.out").resolve()
                    if fallback.exists() and fallback.is_file() and (not fallback.is_symlink()):
                        output_blob = fallback.read_bytes()
                if output_blob is None:
                    detail = str(case_result.get("feedback") or "").strip() or str(summary_obj.get("error") or "").strip()
                    if not detail:
                        detail = "judge backend did not produce generated input output"
                    results.append((1, detail))
                    continue
                results.append((0, output_blob.decode("utf-8", errors="replace")))
            return results

        def _materialize_noncpp_target(*, source: Path, output: Path, source_bytes: bytes, artifact_bytes: bytes | None) -> None:
            suffix = source.suffix.lower()
            output.parent.mkdir(parents=True, exist_ok=True)
            if suffix == ".py":
                launcher = artifact_bytes if artifact_bytes else (
                    "#!/bin/sh\n"
                    "set -eu\n"
                    "HERE=\"$(CDPATH= cd -- \"$(dirname \"$0\")\" && pwd)\"\n"
                    "SCRIPT_NAME=\"$(basename \"$0\").py\"\n"
                    "PY=\"\"\n"
                    "if command -v python3 >/dev/null 2>&1; then\n"
                    "  PY=\"python3\"\n"
                    "elif command -v python >/dev/null 2>&1; then\n"
                    "  PY=\"python\"\n"
                    "elif command -v pypy3 >/dev/null 2>&1; then\n"
                    "  PY=\"pypy3\"\n"
                    "fi\n"
                    "if [ -z \"$PY\" ]; then\n"
                    "  echo \"python interpreter not found\" >&2\n"
                    "  exit 1\n"
                    "fi\n"
                    "export HOME=/does/not/exist\n"
                    "exec \"$PY\" \"$HERE/$SCRIPT_NAME\" \"$@\"\n"
                ).encode("utf-8")
                output.write_bytes(launcher)
                output.chmod(0o755)
                output.with_name(f"{output.name}.py").write_bytes(source_bytes)
                return
            if suffix == ".java":
                source_text = source_bytes.decode("utf-8", errors="replace")
                class_match = re.search(
                    r"^[ \t]*public[ \t]+class[ \t]+([A-Za-z_][A-Za-z0-9_]*)",
                    source_text,
                    flags=re.MULTILINE,
                )
                java_name = f"{class_match.group(1)}.java" if class_match else source.name
                java_source = output.with_name(java_name)
                java_source.write_bytes(source_bytes)
                launcher = (
                    "#!/bin/sh\n"
                    "set -eu\n"
                    "HERE=\"$(CDPATH= cd -- \"$(dirname \"$0\")\" && pwd)\"\n"
                    f"exec java \"$HERE/{java_name}\" \"$@\"\n"
                )
                output.write_text(launcher, encoding="utf-8")
                output.chmod(0o755)
                return
            raise RuntimeError(f"unsupported source language: {suffix or '(no extension)'}")

        def _compile_via_judgehost_target(name: str, source: Path, output: Path) -> tuple[bool, str, str, str]:
            source_bytes = source.read_bytes()
            run_id = f"r-bc-{uuid.uuid4().hex[:12]}"
            invocation_id = f"inv-buildcompile-{build_id[:12]}-{uuid.uuid4().hex[:8]}"
            prepared_payload: dict[str, object] | None = None
            if source.suffix.lower() in CPP_EXTENSIONS:
                extra_sources: dict[str, str] = {}
                workspace_testlib = (snapshot / "third_party" / "testlib" / "testlib.h").resolve()
                if workspace_testlib.exists() and workspace_testlib.is_file():
                    extra_sources["testlib.h"] = base64.b64encode(workspace_testlib.read_bytes()).decode("ascii")
                else:
                    upstream_testlib = (
                        Path(__file__).resolve().parents[3] / "third_party" / "upstream" / "testlib" / "testlib.h"
                    ).resolve()
                    if upstream_testlib.exists() and upstream_testlib.is_file():
                        extra_sources["testlib.h"] = base64.b64encode(upstream_testlib.read_bytes()).decode("ascii")
                if extra_sources:
                    prepared_payload = {"extra_sources_b64": extra_sources}
            if prepared_payload is not None:
                task_id = compile_backend.enqueue_compile_only_task(
                    problem=problem,
                    username=username,
                    build_id=build_id,
                    upload_content=source_bytes,
                    upload_filename=source.name,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    invocation_run_ids=[run_id],
                    expected_behavior="compile",
                    invocation_source="build.compile",
                    prepared_payload=prepared_payload,
                )
            else:
                task_id = compile_backend.enqueue_compile_only_task(
                    problem=problem,
                    username=username,
                    build_id=build_id,
                    upload_content=source_bytes,
                    upload_filename=source.name,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    invocation_run_ids=[run_id],
                    expected_behavior="compile",
                    invocation_source="build.compile",
                )
            waited_run_id = str(compile_backend.wait_for_task(task_id, timeout_sec=None) or run_id).strip() or run_id
            run_row = self.db.fetch_one(
                "SELECT status,summary_json,artifact_path FROM runs WHERE id=?",
                [waited_run_id],
            )
            if run_row is None:
                return (False, "", "judge backend compile result missing", "judgehost")
            run_status = str(run_row["status"] or "").strip().lower()
            summary_obj: dict = {}
            raw_summary = str(run_row["summary_json"] or "").strip()
            if raw_summary:
                try:
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, dict):
                        summary_obj = parsed
                except Exception:
                    summary_obj = {}
            if run_status and run_status != "ok":
                failure_text = str(summary_obj.get("error") or "").strip() or "judge backend compile failed"
                return (False, "", failure_text, "judgehost")
            compile_error = _first_compile_message(summary_obj)
            verdict = _run_summary_verdict(summary_obj)
            if verdict == "CE":
                return (False, "", compile_error or f"compile failed: {name}", "judgehost")
            if verdict and verdict != "OK":
                return (False, "", compile_error or f"compile failed: {name} ({verdict})", "judgehost")

            output_blob: bytes | None = None
            output_ref = _run_summary_output_ref(summary_obj)
            work_root_hint = _run_summary_work_root(summary_obj)
            if output_ref:
                try:
                    output_blob = compile_backend.resolve_artifact_blob(output_ref, work_root=work_root_hint)
                except Exception:
                    output_blob = None
            if output_blob is None:
                run_root = Path(str(run_row["artifact_path"] or "")).resolve()
                fallback = (run_root / "001.out").resolve()
                if fallback.exists() and fallback.is_file() and (not fallback.is_symlink()):
                    output_blob = fallback.read_bytes()

            if source.suffix.lower() in CPP_EXTENSIONS:
                if not output_blob:
                    return (False, "", "missing compile artifact", "judgehost")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(output_blob)
                output.chmod(0o755)
            else:
                _materialize_noncpp_target(
                    source=source,
                    output=output,
                    source_bytes=source_bytes,
                    artifact_bytes=output_blob,
                )
            return (True, "", "", "judgehost")

        if (not verification_pipeline) and compile_plan:
            with ThreadPoolExecutor(max_workers=compile_jobs) as pool:
                future_map = {
                    pool.submit(_compile_via_judgehost_target, name, source, output): name
                    for name, source, output in compile_plan
                }
                for future in as_completed(future_map):
                    name = future_map[future]
                    compile_results[name] = future.result()

        compiled_bins: dict[str, Path] = {}
        compile_log_path = logs_dir / "compile.log"
        with compile_log_path.open("w", encoding="utf-8") as clog:
            clog.write(f"compile_jobs={compile_jobs}\n")
            if verification_pipeline:
                clog.write("compile_strategy=source-only (verification pipeline)\n")
            for name, source, output in compile_targets:
                if source is None:
                    clog.write(f"[{name}] missing source\n\n")
                    continue
                clog.write(f"[{name}] source={source}\n")
                if verification_pipeline:
                    clog.write("compile skipped: verification pipeline uses judgehost generate/solve task model\n\n")
                    continue
                ok, out, err, toolchain_digest = compile_results[name]
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

        if verification_pipeline:
            if build_cfg.get("require_validator", True) and (compile_source_by_name.get("validator") is None):
                raise RuntimeError("validator source is required")
            if build_cfg.get("require_checker", True) and (compile_source_by_name.get("checker") is None):
                raise RuntimeError("checker source is required")
            if compile_source_by_name.get("accepted_solution") is None:
                raise RuntimeError("accepted solution source is required")
            if interactive_mode and (compile_source_by_name.get("interactor") is None):
                raise RuntimeError("interactor source is required for interactive mode")
        else:
            if build_cfg.get("require_validator", True) and "validator" not in compiled_bins:
                raise RuntimeError("validator source is required")
            if build_cfg.get("require_checker", True) and "checker" not in compiled_bins:
                raise RuntimeError("checker source is required")
            if "accepted_solution" not in compiled_bins:
                raise RuntimeError("accepted solution source is required")
            if interactive_mode and "interactor" not in compiled_bins:
                raise RuntimeError("interactor source is required for interactive mode")

        steps.append({"step": "compile", "status": "ok", "log": "logs/compile.log"})

        current_step = "generate"
        test_files: list[Path] = []
        tests_meta: list[dict] = []
        source_answer_by_test: dict[str, Path] = {}
        counter = 1
        manual_count = 0
        generated_count = 0
        generate_log_path = logs_dir / "generate.log"
        with generate_log_path.open("w", encoding="utf-8") as glog:
            if tests_spec_entries is not None:
                gen_batch_args_by_source: dict[Path, list[list[str]]] = {}
                for planned in tests_spec_runtime:
                    if str(planned.get("kind") or "") != "gen":
                        continue
                    target_name = str(planned.get("target_name") or "")
                    planned_source = generator_source_by_name.get(target_name)
                    if planned_source is None:
                        continue
                    planned_args = [str(item or "") for item in (planned.get("args") or [])]
                    gen_batch_args_by_source.setdefault(planned_source, []).append(planned_args)
                gen_batch_results_by_source: dict[Path, list[tuple[int, str]]] = {}
                gen_batch_next_index_by_source: dict[Path, int] = {}
                glog.write("tests_source=tests/spec.json\n")
                for row in tests_spec_runtime:
                    kind = str(row.get("kind") or "")
                    test_id = str(row.get("id") or "").strip()
                    is_sample = bool(row.get("sample"))
                    if sample_only and (not is_sample):
                        continue
                    custom_sample_input = str(row.get("sample_input") or "")
                    custom_sample_output = str(row.get("sample_output") or "")
                    custom_sample_output_validate = bool(row.get("sample_output_validate", True))
                    file_index = int(row.get("index") or counter) if sample_only else counter
                    dst = artifact_paths.tests / f"{file_index:03d}.in"
                    if kind == "manual":
                        input_text = str(row.get("input") or "")
                        dst.write_text(input_text, encoding="utf-8")
                        test_files.append(dst)
                        manual_count += 1
                        tests_meta.append(
                            {
                                "index": file_index,
                                "kind": "manual",
                                "id": test_id,
                                "sample": is_sample,
                                "sample_input_custom": bool(custom_sample_input),
                                "sample_output_custom": bool(custom_sample_output),
                                "sample_output_validate": bool(custom_sample_output_validate),
                                "desc": f"manual {test_id}" if test_id else "manual",
                                "source": str(row.get("source_rel") or "tests/spec.json"),
                            }
                        )
                        if is_sample and custom_sample_output:
                            custom_sample_rows_by_test[dst.name] = {
                                "id": test_id,
                                "sample_input": custom_sample_input,
                                "sample_output": custom_sample_output,
                                "sample_output_validate": custom_sample_output_validate,
                            }
                        answer_source = tests_spec_answer_source(snapshot, test_id)
                        if answer_source is not None:
                            source_answer_by_test[dst.name] = answer_source
                        glog.write(f"manual id={test_id} index={row.get('index')} -> {dst.name}\n")
                        if not sample_only:
                            counter += 1
                        continue

                    if kind != "gen":
                        raise RuntimeError(f"invalid test kind at tests/spec.json entry {row.get('index')}")
                    target_name = str(row.get("target_name") or "")
                    gen_source = generator_source_by_name.get(target_name)
                    if gen_source is None:
                        raise RuntimeError(
                            f"generator source is required for tests/spec.json entry {row.get('index')}"
                        )
                    args = [str(x) for x in row.get("args") or []]
                    if gen_source not in gen_batch_results_by_source:
                        planned_args_list = list(gen_batch_args_by_source.get(gen_source) or [])
                        gen_batch_results_by_source[gen_source] = _run_generator_inputs_via_judgehost(
                            generator_source=gen_source,
                            args_list=planned_args_list,
                        )
                        gen_batch_next_index_by_source[gen_source] = 0
                    next_index = int(gen_batch_next_index_by_source.get(gen_source, 0))
                    batched_results = list(gen_batch_results_by_source.get(gen_source) or [])
                    if 0 <= next_index < len(batched_results):
                        rc, output_or_err = batched_results[next_index]
                    else:
                        rc, output_or_err = (1, "judge backend generate result missing")
                    gen_batch_next_index_by_source[gen_source] = next_index + 1
                    glog.write(
                        f"gen id={test_id} index={row.get('index')} source={row.get('source_rel')} cmd={row.get('cmd')} batch_index={next_index + 1} rc={rc}\n{output_or_err if rc != 0 else ''}\n"
                    )
                    if rc != 0:
                        dst.unlink(missing_ok=True)
                        failing_test = dst.name
                        raise RuntimeError(
                            f"generator failed on tests/spec.json entry {row.get('index')} (id={test_id}): {output_or_err}"
                        )
                    dst.write_text(output_or_err, encoding="utf-8")
                    test_files.append(dst)
                    generated_count += 1
                    desc = str(row.get("cmd") or "").strip() or "gen"
                    tests_meta.append(
                        {
                            "index": file_index,
                            "kind": "gen",
                            "id": test_id,
                            "sample": is_sample,
                            "sample_input_custom": bool(custom_sample_input),
                            "sample_output_custom": bool(custom_sample_output),
                            "sample_output_validate": bool(custom_sample_output_validate),
                            "desc": desc,
                            "command": str(row.get("cmd") or "").strip(),
                            "source": str(row.get("source_rel") or "").strip(),
                            "payload_source": str(row.get("payload_rel") or "").strip(),
                        }
                    )
                    if is_sample and custom_sample_output:
                        custom_sample_rows_by_test[dst.name] = {
                            "id": test_id,
                            "sample_input": custom_sample_input,
                            "sample_output": custom_sample_output,
                            "sample_output_validate": custom_sample_output_validate,
                        }
                    answer_source = tests_spec_answer_source(snapshot, test_id)
                    if answer_source is not None:
                        source_answer_by_test[dst.name] = answer_source
                    if not sample_only:
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
                    if source is None:
                        continue
                    try:
                        source_label = str(source.relative_to(snapshot)).replace("\\", "/")
                    except ValueError:
                        source_label = str(source)
                    generator_execs.append((gen_index, source_label, source))

                if generator_execs:
                    runs = int(build_cfg.get("generator_runs", 3))
                    generator_args = [str(x) for x in build_cfg.get("generator_args", [])]
                    for gen_index, source_label, gen_source in generator_execs:
                        args_batch = [list(generator_args) for _ in range(max(0, runs))]
                        batched_results = _run_generator_inputs_via_judgehost(
                            generator_source=gen_source,
                            args_list=args_batch,
                        )
                        for i in range(runs):
                            dst = artifact_paths.tests / f"{counter:03d}.in"
                            if i < len(batched_results):
                                rc, output_or_err = batched_results[i]
                            else:
                                rc, output_or_err = (1, "judge backend generate result missing")
                            glog.write(
                                f"generator={gen_index} source={source_label} case={i + 1} rc={rc}\n{output_or_err if rc != 0 else ''}\n"
                            )
                            if rc != 0:
                                dst.unlink(missing_ok=True)
                                failing_test = dst.name
                                raise RuntimeError(
                                    f"generator failed on generator={gen_index} case={i + 1}: {output_or_err}"
                                )
                            dst.write_text(output_or_err, encoding="utf-8")
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
                if sample_only:
                    raise RuntimeError("no sample tests were generated from tests/spec.json")
                raise RuntimeError("no tests were generated from tests/spec.json")
            raise RuntimeError("no tests were generated (manual + generator)")
        (logs_dir / "tests_meta.json").write_text(json.dumps(tests_meta, indent=2), encoding="utf-8")
        steps.append({"step": "generate", "status": "ok", "log": "logs/generate.log"})

        current_step = "validate"
        with (logs_dir / "validate.log").open("w", encoding="utf-8") as vlog:
            vlog.write("validation is performed in judgehost generate pipeline; local native validator execution disabled\n")
        steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

        current_step = "solve"
        solve_jobs = self._effective_compile_jobs(build_cfg.get("solve_jobs", 0), len(test_files))
        custom_sample_output_validate_total = 0
        custom_sample_output_validate_checked = 0
        solve_results: dict[str, dict[str, object]] = {}
        solve_backend = "domjudge-judgehost"
        use_judge_backend = self._can_use_judge_backend_for_solve()
        with (logs_dir / "solve.log").open("w", encoding="utf-8") as slog:
            slog.write(f"solve_jobs={solve_jobs}\n")
            slog.write(f"solve_backend={solve_backend}\n")
            slog.write(f"build_solve_timeout_sec={build_solve_timeout_sec}\n")

            def _solve_failure_message(test_name: str, row: dict[str, object]) -> str:
                def _main_status_token(result_row: dict[str, object]) -> str:
                    verdict = str(result_row.get("verdict") or "").strip().upper()
                    if verdict in {"OK", "AC", "ACCEPTED", "CORRECT"}:
                        return "AC"
                    if verdict.startswith("TL"):
                        return "TL"
                    if verdict in {"WA", "WRONG-ANSWER", "WRONG_ANSWER"}:
                        return "WA"
                    if verdict in {"RE", "RUN-ERROR", "RUN_ERROR", "RUNTIME-ERROR", "RUNTIME_ERROR"}:
                        return "RE"
                    if verdict in {"CE", "COMPILER-ERROR", "COMPILER_ERROR"}:
                        return "CE"
                    if verdict in {"FL", "FAIL", "FAILED", "INTERNAL-ERROR", "INTERNAL_ERROR", "COMPARE-ERROR", "COMPARE_ERROR"}:
                        return "FL"
                    if bool(result_row.get("timed_out")):
                        return "TL"
                    rc_token = int(result_row.get("rc") or 0)
                    if rc_token != 0:
                        return "FL"
                    return ""

                rc = int(row.get("rc") or 0)
                worker_error = str(row.get("worker_error") or "").strip()
                timed_out = bool(row.get("timed_out"))
                stderr_text = compact_single_line(str(row.get("stderr") or ""), 220)
                status_token = _main_status_token(row)
                if worker_error:
                    if status_token and status_token != "AC":
                        return f"main correct solution {status_token} on {test_name}: {worker_error}"
                    return f"main correct solution failed on {test_name}: {worker_error}"
                if rc == 0:
                    return ""
                if status_token and status_token != "AC":
                    base_msg = f"main correct solution {status_token} on {test_name}"
                else:
                    base_msg = f"main correct solution failed on {test_name}"
                detail_text = f"rc={rc}, timed_out=1" if timed_out else f"rc={rc}"
                if stderr_text:
                    detail_text = f"{detail_text}: stderr: {stderr_text}"
                return f"{base_msg}: {detail_text}"

            if not use_judge_backend:
                msg = "judge backend unavailable for build solve; configure JUDGEHOST_ENABLE and JUDGEHOST_API_TOKEN"
                slog.write(msg + "\n")
                raise RuntimeError(msg)
            solve_results = self._solve_with_judge_backend(
                problem=problem,
                username=username,
                build_id=build_id,
                accepted_source_rel=accepted_rel,
                mode=problem_mode,
                test_files=test_files,
                ans_dir=artifact_paths.ans,
                solve_jobs=solve_jobs,
                source_answer_by_test=source_answer_by_test,
            )
            for t in test_files:
                row = solve_results.get(t.name) or self._solve_result_error("missing judge solve result")
                solve_results[t.name] = row
                rc = int(row.get("rc") or 0)
                timed_out = bool(row.get("timed_out"))
                err = str(row.get("worker_error") or row.get("stderr") or "")
                timeout_note = " timed_out=1" if timed_out else ""
                slog.write(f"{t.name}: rc={rc}{timeout_note}\n{err}\n")
                fail_msg = _solve_failure_message(t.name, row)
                if fail_msg:
                    failing_test = t.name
                    slog.write(f"early_stop={t.name}\n")
                    raise RuntimeError(fail_msg)

            for t in test_files:
                failing_test = t.name
                row = solve_results[t.name]
                fail_msg = _solve_failure_message(t.name, row)
                if fail_msg:
                    raise RuntimeError(fail_msg)

            if custom_sample_rows_by_test:
                custom_validate_log = logs_dir / "sample_output_validate.log"
                with custom_validate_log.open("w", encoding="utf-8") as cvlog:
                    cvlog.write("sample custom output validation via local native execution is disabled\n")
                    cvlog.write("use judgehost verification pipeline for checker validation\n")
                if custom_sample_output_validate_total > 0:
                    steps.append(
                        {
                            "step": "sample_output_validate",
                            "status": "ok",
                            "log": "logs/sample_output_validate.log",
                        }
                    )
        steps.append({"step": "solve", "status": "ok", "log": "logs/solve.log"})

        (logs_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
        generation_params = {
            "tests_spec_enabled": tests_spec_entries is not None,
            "tests_spec_entries": len(tests_spec_runtime) if tests_spec_entries is not None else 0,
            "tests_spec_sample_custom_output_validate_total": custom_sample_output_validate_total,
            "tests_spec_sample_custom_output_validate_checked": custom_sample_output_validate_checked,
            "generator_runs": int(build_cfg.get("generator_runs", 3)),
            "compile_jobs": compile_jobs,
            "validate_jobs": int(build_cfg.get("validate_jobs", 0)),
            "validate_jobs_effective": 0,
            "solve_jobs": int(build_cfg.get("solve_jobs", 0)),
            "solve_jobs_effective": solve_jobs,
            "run_jobs": int(build_cfg.get("run_jobs", 0)),
            "mode": problem_mode,
            "sample_only": bool(sample_only),
            "build_ref": build_ref,
            "solve_backend": solve_backend,
            "time_limit_ms": time_limit_ms,
            "run_timeout_ms": run_timeout_ms,
            "run_timeout_sec": run_timeout_sec,
            "generator_sources": [str(x) for x in build_cfg.get("generator_sources", [])],
            "generator_args": [str(x) for x in build_cfg.get("generator_args", [])],
            "validator_args": [str(x) for x in build_cfg.get("validator_args", [])],
            "checker_args": [str(x) for x in build_cfg.get("checker_args", [])],
            "checker_standard": str(build_cfg.get("checker_standard", "")),
            "max_passes": int(build_cfg.get("max_passes", 16)),
            "sandbox_backend": self.sandbox.name,
            "sandbox_memory_mb": self.default_exec_memory_mb,
            "sandbox_process_limit": self.default_exec_process_limit,
            "sandbox_output_kb": self.default_exec_output_kb,
            "generation_params_digest": str(generation_params_digest or "").strip().lower(),
            "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            "verification_pipeline": bool(verification_pipeline),
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
            [
                "ok",
                summary_for_db(
                    {
                        "build_ref": build_ref,
                        "steps": steps,
                        "diagnostics": diagnostics,
                        "generation_params": generation_params,
                    },
                    normalize_diagnostics_for_db=normalize_diagnostics_for_db,
                    diagnostics_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
                ),
                now_iso(),
                build_id,
            ],
        )
        if use_build_result_cache and self._async_task_cache_service is not None and str(source_commit or "").strip():
            self._async_task_cache_service.put(
                self.BUILD_CACHE_NAMESPACE,
                cache_key
                if isinstance(cache_key, dict)
                else self._build_cache_key(
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    source_commit=str(source_commit or "").strip(),
                    source_ref=str(source_ref or "").strip(),
                    generation_params_digest=str(generation_params_digest or "").strip().lower(),
                    toolchain_cmd_digest=str(toolchain_cmd_digest or "").strip().lower(),
                    sample_only=bool(sample_only),
                ),
                {"build_id": build_id},
                tags={
                    "problem_id": str(problem_id),
                    "workspace_id": str(workspace_id),
                    "source_commit": str(source_commit or "").strip(),
                    "sample_only": "1" if sample_only else "0",
                },
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
                summary_for_db(
                    {
                        "build_ref": build_ref,
                        "error": str(exc),
                        "failed_step": current_step,
                        "failed_test": failing_test,
                        "steps": steps,
                        "diagnostics": diagnostics,
                    },
                    normalize_diagnostics_for_db=normalize_diagnostics_for_db,
                    diagnostics_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
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
        if inflight_owner and cache_key_hash:
            with self._build_inflight_lock:
                current = str(self._build_inflight.get(cache_key_hash) or "").strip()
                if current == build_id:
                    self._build_inflight.pop(cache_key_hash, None)

    return build_id

