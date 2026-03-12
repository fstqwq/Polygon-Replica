from __future__ import annotations

import base64
import json
from pathlib import Path
import random
import re
import shlex
import shutil
import uuid

from app.db import now_iso
from app.service.platform.process import run_cmd
from app.service.platform.testlib_source import workspace_testlib_header
from app.service.build.diagnostic import compact_single_line, normalize_diagnostics_for_db
from app.service.build.source import resolve_source
from app.service.build.summary import summary_for_db
from app.service.build.test_spec import tests_spec_answer_source
from app.service.run.runtime import RUN_TEST_NAME_RE

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
        shared_testlib_blob: bytes | None = None
        resolved_testlib = workspace_testlib_header(snapshot)
        if resolved_testlib is not None:
            shared_testlib_blob = resolved_testlib.read_bytes()

        compile_jobs = 0
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
            *,
            source_name: str,
            source_bytes: bytes,
            tests_payload: list[dict[str, str]],
            extra_sources_b64: dict[str, str] | None = None,
            manual_validate_only: bool = False,
        ) -> dict[str, tuple[int, str]]:
            safe_source_name = Path(str(source_name or "").strip() or "submission.cpp").name
            normalized_tests: list[dict[str, str]] = []
            for row in tests_payload:
                if not isinstance(row, dict):
                    continue
                test_name = Path(str(row.get("name") or "").strip()).name
                if not RUN_TEST_NAME_RE.fullmatch(test_name):
                    continue
                normalized_tests.append(
                    {
                        "name": test_name,
                        "input_b64": str(row.get("input_b64") or ""),
                        "answer_name": str(row.get("answer_name") or f"{Path(test_name).stem}.ans"),
                        "answer_b64": str(row.get("answer_b64") or ""),
                    }
                )
            if not normalized_tests:
                return {}
            run_id = f"r-bg-{uuid.uuid4().hex[:12]}"
            invocation_id = f"inv-buildgen-{build_id[:12]}-{uuid.uuid4().hex[:8]}"
            validator_source = compile_source_by_name.get("validator")
            sources_payload: dict[str, str] = {}
            binaries_payload: dict[str, str] = {}
            submission_extra_sources_payload: dict[str, str] = {}

            if isinstance(validator_source, Path) and validator_source.exists() and validator_source.is_file():
                sources_payload["validator.cpp"] = base64.b64encode(validator_source.read_bytes()).decode("ascii")
                if shared_testlib_blob is not None:
                    sources_payload["testlib.h"] = base64.b64encode(shared_testlib_blob).decode("ascii")
            if isinstance(extra_sources_b64, dict):
                for raw_name, raw_blob in extra_sources_b64.items():
                    safe_name = Path(str(raw_name or "").strip()).name
                    if not safe_name:
                        continue
                    submission_extra_sources_payload[safe_name] = str(raw_blob or "")
                    if safe_name in sources_payload:
                        continue
                    sources_payload[safe_name] = str(raw_blob or "")

            checker_args: list[str] = []
            if manual_validate_only:
                checker_args.append("--validate-input")

            prepared_payload: dict[str, object] = {
                "build_payload": {
                    "tests": normalized_tests,
                    "run_config_json": json.dumps(
                        {
                            "checker_mode": "testlib",
                            "checker_args": checker_args,
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
                },
                "extra_sources_b64": submission_extra_sources_payload,
                "manual_validate_only": bool(manual_validate_only),
            }

            task_id = compile_backend.enqueue_task(
                problem=problem,
                username=username,
                build_id=build_id,
                mode=problem_mode,
                submission_path=None,
                upload_content=source_bytes,
                upload_filename=safe_source_name,
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
                return {
                    row["name"]: (1, "judge backend generate result missing")
                    for row in normalized_tests
                }
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
                return {row["name"]: (1, detail) for row in normalized_tests}
            run_root = Path(str(run_row["artifact_path"] or "")).resolve()
            work_root_hint = _run_summary_work_root(summary_obj)
            test_result_map = _run_summary_test_result_map(summary_obj)
            results: dict[str, tuple[int, str]] = {}
            for row in normalized_tests:
                case_name = str(row.get("name") or "").strip()
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
                    results[case_name] = (1, detail)
                    continue
                if manual_validate_only:
                    results[case_name] = (0, "")
                    continue
                output_ref = str(case_result.get("output_ref") or "").strip()
                output_blob: bytes | None = None
                if output_ref:
                    try:
                        output_blob = compile_backend.resolve_artifact_blob(output_ref, work_root=work_root_hint)
                    except Exception:
                        output_blob = None
                if output_blob is None:
                    fallback = (run_root / f"{Path(case_name).stem}.out").resolve()
                    if fallback.exists() and fallback.is_file() and (not fallback.is_symlink()):
                        output_blob = fallback.read_bytes()
                if output_blob is None:
                    detail = str(case_result.get("feedback") or "").strip() or str(summary_obj.get("error") or "").strip()
                    if not detail:
                        detail = "judge backend did not produce generated input output"
                    results[case_name] = (1, detail)
                    continue
                results[case_name] = (0, output_blob.decode("utf-8", errors="replace"))
            return results

        compile_log_path = logs_dir / "compile.log"
        with compile_log_path.open("w", encoding="utf-8") as clog:
            clog.write("compile_jobs=0\n")
            clog.write("compile_strategy=judgehost-source-only\n")
            for name, source, output in compile_targets:
                if source is None:
                    clog.write(f"[{name}] missing source\n\n")
                    continue
                clog.write(f"[{name}] source={source}\n")
                clog.write("compile skipped: build uses judgehost source-only generate/solve task model\n\n")

        if build_cfg.get("require_validator", True) and (compile_source_by_name.get("validator") is None):
            raise RuntimeError("validator source is required")
        if build_cfg.get("require_checker", True) and (compile_source_by_name.get("checker") is None):
            raise RuntimeError("checker source is required")
        if compile_source_by_name.get("accepted_solution") is None:
            raise RuntimeError("accepted solution source is required")
        if interactive_mode and (compile_source_by_name.get("interactor") is None):
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
            planned_rows: list[dict[str, object]] = []
            if tests_spec_entries is not None:
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
                        input_bytes = str(row.get("input") or "").encode("utf-8")
                        planned_rows.append(
                            {
                                "kind": "manual",
                                "dst": dst,
                                "input_bytes": input_bytes,
                                "tests_meta": {
                                    "index": file_index,
                                    "kind": "manual",
                                    "id": test_id,
                                    "sample": is_sample,
                                    "sample_input_custom": bool(custom_sample_input),
                                    "sample_output_custom": bool(custom_sample_output),
                                    "sample_output_validate": bool(custom_sample_output_validate),
                                    "desc": f"manual {test_id}" if test_id else "manual",
                                    "source": str(row.get("source_rel") or "tests/spec.json"),
                                },
                                "log_prefix": f"manual id={test_id} index={row.get('index')}",
                                "error_context": f"tests/spec.json entry {row.get('index')} (id={test_id})",
                                "custom_sample_row": {
                                    "id": test_id,
                                    "sample_input": custom_sample_input,
                                    "sample_output": custom_sample_output,
                                    "sample_output_validate": custom_sample_output_validate,
                                }
                                if is_sample and custom_sample_output
                                else None,
                                "answer_source": tests_spec_answer_source(snapshot, test_id),
                            }
                        )
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
                    desc = str(row.get("cmd") or "").strip() or "gen"
                    command_payload = " ".join(
                        ['"$SUBMISSION_BIN"', *[shlex.quote(str(item or "")) for item in args]]
                    ).strip()
                    if not command_payload:
                        command_payload = '"$SUBMISSION_BIN"'
                    planned_rows.append(
                        {
                            "kind": "gen",
                            "dst": dst,
                            "generator_source": gen_source,
                            "command_payload": command_payload,
                            "tests_meta": {
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
                            },
                            "log_prefix": f"gen id={test_id} index={row.get('index')} source={row.get('source_rel')} cmd={row.get('cmd')}",
                            "error_context": f"tests/spec.json entry {row.get('index')} (id={test_id})",
                            "custom_sample_row": {
                                "id": test_id,
                                "sample_input": custom_sample_input,
                                "sample_output": custom_sample_output,
                                "sample_output_validate": custom_sample_output_validate,
                            }
                            if is_sample and custom_sample_output
                            else None,
                            "answer_source": tests_spec_answer_source(snapshot, test_id),
                        }
                    )
                    if not sample_only:
                        counter += 1
            else:
                tests = self._manual_test_sources(snapshot)
                for t in tests:
                    dst = artifact_paths.tests / f"{counter:03d}.in"
                    try:
                        source_rel = str(t.relative_to(snapshot)).replace("\\", "/")
                    except ValueError:
                        source_rel = str(t.name)
                    planned_rows.append(
                        {
                            "kind": "manual",
                            "dst": dst,
                            "input_bytes": t.read_bytes(),
                            "tests_meta": {
                                "index": counter,
                                "kind": "manual",
                                "desc": f"manual: {source_rel}",
                                "source": source_rel,
                            },
                            "log_prefix": f"manual source={source_rel}",
                            "error_context": source_rel,
                            "custom_sample_row": None,
                            "answer_source": None,
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
                        for i in range(runs):
                            dst = artifact_paths.tests / f"{counter:03d}.in"
                            desc = f"gen: {source_label}"
                            if generator_args:
                                desc = f"{desc} {' '.join(generator_args)}"
                            command_payload = " ".join(
                                ['"$SUBMISSION_BIN"', *[shlex.quote(str(item or "")) for item in generator_args]]
                            ).strip()
                            if not command_payload:
                                command_payload = '"$SUBMISSION_BIN"'
                            planned_rows.append(
                                {
                                    "kind": "gen",
                                    "dst": dst,
                                    "generator_source": gen_source,
                                    "command_payload": command_payload,
                                    "tests_meta": {
                                        "index": counter,
                                        "kind": "gen",
                                        "desc": desc,
                                        "source": source_label,
                                    },
                                    "log_prefix": f"generator={gen_index} source={source_label} case={i + 1}",
                                    "error_context": f"generator={gen_index} case={i + 1}",
                                    "custom_sample_row": None,
                                    "answer_source": None,
                                }
                            )
                            counter += 1

            manual_batch_payload: list[dict[str, str]] = []
            manual_batch_rows: list[dict[str, object]] = []
            gen_batch_payloads_by_source: dict[Path, list[dict[str, str]]] = {}
            gen_batch_rows_by_source: dict[Path, list[dict[str, object]]] = {}
            for planned in planned_rows:
                dst = planned["dst"]
                if not isinstance(dst, Path):
                    continue
                if str(planned.get("kind") or "") == "manual":
                    manual_batch_rows.append(planned)
                    manual_batch_payload.append(
                        {
                            "name": dst.name,
                            "input_b64": base64.b64encode(bytes(planned.get("input_bytes") or b"")).decode("ascii"),
                            "answer_name": f"{dst.stem}.ans",
                            "answer_b64": "",
                        }
                    )
                    continue
                gen_source = planned.get("generator_source")
                if not isinstance(gen_source, Path):
                    continue
                gen_batch_rows_by_source.setdefault(gen_source, []).append(planned)
                gen_batch_payloads_by_source.setdefault(gen_source, []).append(
                    {
                        "name": dst.name,
                        "input_b64": base64.b64encode(
                            (str(planned.get("command_payload") or "") + "\n").encode("utf-8")
                        ).decode("ascii"),
                        "answer_name": f"{dst.stem}.ans",
                        "answer_b64": "",
                    }
                )

            manual_results_by_name: dict[str, tuple[int, str]] = {}
            if manual_batch_payload:
                manual_results_by_name = _run_generator_inputs_via_judgehost(
                    source_name="manual_validate.cpp",
                    source_bytes=b"int main(){return 0;}\n",
                    tests_payload=manual_batch_payload,
                    manual_validate_only=True,
                )

            gen_results_by_name: dict[str, tuple[int, str]] = {}
            for gen_source, payload_rows in gen_batch_payloads_by_source.items():
                extra_sources_b64: dict[str, str] = {}
                if gen_source.suffix.lower() in CPP_EXTENSIONS and shared_testlib_blob is not None:
                    extra_sources_b64["testlib.h"] = base64.b64encode(shared_testlib_blob).decode("ascii")
                source_results = _run_generator_inputs_via_judgehost(
                    source_name=gen_source.name,
                    source_bytes=gen_source.read_bytes(),
                    tests_payload=payload_rows,
                    extra_sources_b64=extra_sources_b64,
                    manual_validate_only=False,
                )
                gen_results_by_name.update(source_results)

            for planned in planned_rows:
                dst = planned["dst"]
                if not isinstance(dst, Path):
                    continue
                kind = str(planned.get("kind") or "")
                result_map = manual_results_by_name if kind == "manual" else gen_results_by_name
                rc, output_or_err = result_map.get(dst.name, (1, "judge backend generate result missing"))
                glog.write(f"{planned.get('log_prefix')} -> {dst.name} rc={rc}\n")
                if rc != 0:
                    if output_or_err:
                        glog.write(str(output_or_err) + "\n")
                    dst.unlink(missing_ok=True)
                    failing_test = dst.name
                    action = "validator failed on" if kind == "manual" else "generator failed on"
                    raise RuntimeError(f"{action} {planned.get('error_context')}: {output_or_err}")
                if kind == "manual":
                    dst.write_bytes(bytes(planned.get("input_bytes") or b""))
                    manual_count += 1
                else:
                    dst.write_text(str(output_or_err or ""), encoding="utf-8")
                    generated_count += 1
                test_files.append(dst)
                tests_meta.append(dict(planned.get("tests_meta") or {}))
                custom_sample_row = planned.get("custom_sample_row")
                if isinstance(custom_sample_row, dict):
                    custom_sample_rows_by_test[dst.name] = dict(custom_sample_row)
                answer_source = planned.get("answer_source")
                if isinstance(answer_source, Path):
                    source_answer_by_test[dst.name] = answer_source
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
            vlog.write("input validation is recorded in judgehost build.generate-input runs\n")
            vlog.write(f"validated_tests={len(test_files)}\n")
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
            "sandbox_backend": self.execution_backend_name,
            "sandbox_memory_mb": self.default_exec_memory_mb,
            "sandbox_process_limit": self.default_exec_process_limit,
            "sandbox_output_kb": self.default_exec_output_kb,
            "generation_params_digest": str(generation_params_digest or "").strip().lower(),
            "toolchain_cmd_digest": str(toolchain_cmd_digest or "").strip().lower(),
            "verification_pipeline": False,
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

