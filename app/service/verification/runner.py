from __future__ import annotations

import base64
import json
from pathlib import Path
import random
import shlex
import shutil
import uuid
from typing import Literal, TypedDict, cast

from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior
from app.service.platform.process import run_cmd
from app.service.platform.testlib_source import workspace_testlib_header
from app.service.verification.cache import (
    artifact_ref_from_cache_key_hash,
    ensure_artifact_paths,
    verification_cache_key,
    verification_cache_key_hash,
)
from app.service.verification.judge_solve import solve_result_error, solve_with_judge_backend
from app.service.verification.pipeline import wait_build_terminal_status
from app.service.verification.runtime import normalize_problem_mode
from app.service.verification.test_rows import (
    build_verification_test_row,
    canonicalize_verification_test_rows,
)
from app.service.verification.store import (
    create_verification_record,
    load_verification_record,
    load_verification_summary,
    save_verification_summary,
    verification_update_lock,
)
from app.service.verification.types import Kind, Status
from app.service.verification.diagnostic import compact_single_line
from app.service.verification.source import resolve_source
from app.service.run.summary import summary_for_db
from app.service.verification.test_spec import tests_spec_answer_source
from app.service.run.runtime import RUN_TEST_NAME_RE

CPP_EXTENSIONS = (".cpp", ".cc", ".cxx", ".c++")
SOLUTION_SOURCE_EXTENSIONS = (*CPP_EXTENSIONS, ".py", ".java")
DEFAULT_TIME_LIMIT_MS = 2000


NormalizedTestPayload = TypedDict(
    "NormalizedTestPayload",
    {
        "name": str,
        "input_b64": str,
        "answer_name": str,
        "answer_b64": str,
    },
)

TestsSpecManualRow = TypedDict(
    "TestsSpecManualRow",
    {
        "id": str,
        "sample": bool,
        "sample_input": str,
        "sample_output": str,
        "sample_output_validate": bool,
        "index": int,
        "source_rel": str,
        "kind": Literal["manual"],
        "input": str,
    },
)

TestsSpecGenRow = TypedDict(
    "TestsSpecGenRow",
    {
        "id": str,
        "sample": bool,
        "sample_input": str,
        "sample_output": str,
        "sample_output_validate": bool,
        "index": int,
        "source_rel": str,
        "kind": Literal["gen"],
        "target_name": str,
        "args": list[str],
        "cmd": str,
        "payload_rel": str,
    },
)


TestsSpecRuntimeRow = TestsSpecManualRow | TestsSpecGenRow


CustomSampleRow = TypedDict(
    "CustomSampleRow",
    {
        "id": str,
        "sample_input": str,
        "sample_output": str,
        "sample_output_validate": bool,
    },
)

PlannedManualRow = TypedDict(
    "PlannedManualRow",
    {
        "kind": Literal["manual"],
        "dst": Path,
        "input_bytes": bytes,
        "tests_meta": dict[str, object],
        "log_prefix": str,
        "error_context": str,
        "custom_sample_row": CustomSampleRow | None,
        "answer_source": Path | None,
    },
)

PlannedGenRow = TypedDict(
    "PlannedGenRow",
    {
        "kind": Literal["gen"],
        "dst": Path,
        "generator_source": Path,
        "command_payload": str,
        "tests_meta": dict[str, object],
        "log_prefix": str,
        "error_context": str,
        "custom_sample_row": CustomSampleRow | None,
        "answer_source": Path | None,
    },
)


PlannedRow = PlannedManualRow | PlannedGenRow


VerificationRunRecord = TypedDict(
    "VerificationRunRecord",
    {
        "source_label": str,
        "status": str,
        "artifact_path": str,
        "summary": dict[str, object],
    },
)

SolveStageSummary = TypedDict(
    "SolveStageSummary",
    {
        "verification_source": str,
        "status": str,
        "source": str,
        "artifact_path": str,
        "mode": str,
        "error": str,
        "tests_total": int,
        "tests": list[dict[str, object]],
        "usage": dict[str, object],
        "compile_log": str,
        "compile_diagnostics": list[dict[str, object]],
    },
    total=False,
)

SolveStageResultOut = TypedDict(
    "SolveStageResultOut",
    {
        "artifact_path": str,
        "run_id": str,
        "status": str,
        "summary": dict[str, object],
    },
    total=False,
)

SolveResultRow = TypedDict(
    "SolveResultRow",
    {
        "rc": int,
        "worker_error": str,
        "timed_out": bool,
        "stderr": str,
        "verdict": str,
    },
)

CompileDiagnosticRow = TypedDict(
    "CompileDiagnosticRow",
    {
        "message": str,
    },
    total=False,
)

JudgehostState = TypedDict(
    "JudgehostState",
    {
        "task_id": str,
    },
    total=False,
)

TestPassSummary = TypedDict(
    "TestPassSummary",
    {
        "verdict": str,
        "feedback": str,
        "output_ref": str,
        "output_artifact": str,
        "output_rel": str,
    },
    total=False,
)

TestSummaryRow = TypedDict(
    "TestSummaryRow",
    {
        "test": str,
        "verdict": str,
        "feedback": str,
        "output_ref": str,
        "output_artifact": str,
        "output_rel": str,
        "passes": list[TestPassSummary],
    },
    total=False,
)

JudgehostRunSummary = TypedDict(
    "JudgehostRunSummary",
    {
        "compile_diagnostics": list[CompileDiagnosticRow],
        "error": str,
        "judgehost": JudgehostState,
        "tests": list[TestSummaryRow],
    },
    total=False,
)

JudgehostTaskResult = TypedDict(
    "JudgehostTaskResult",
    {
        "task_status": str,
        "error": str,
        "status": str,
        "summary": JudgehostRunSummary,
        "artifact_path": str,
    },
    total=False,
)

def _default_accepted_solution_source(snapshot: Path) -> str:
    solutions_root = snapshot / "solutions"
    if not solutions_root.exists() or (not solutions_root.is_dir()) or solutions_root.is_symlink():
        return ""
    solution_paths: list[str] = []
    accepted_paths: list[str] = []
    for path in sorted(p for p in solutions_root.rglob("*") if p.is_file()):
        rel = path.relative_to(snapshot).as_posix()
        if Path(rel).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
            continue
        solution_paths.append(rel)
        if normalize_expected_behavior(infer_expected_behavior_from_name(rel)) == "accepted":
            accepted_paths.append(rel)
    if accepted_paths:
        return accepted_paths[0]
    if len(solution_paths) == 1:
        return solution_paths[0]
    return ""


def _promote_solve_main_stage_into_runs(summary: dict[str, object], *, verification_id: str) -> None:
    stage_results = cast(dict[str, SolveStageSummary], summary.get("stage_results") or {})
    solve_stage = stage_results.get("solve_main")
    if solve_stage is None:
        return
    source_token = solve_stage["source"]
    if not source_token:
        return
    runs = cast(dict[str, VerificationRunRecord], summary.get("runs") or {})
    for run_id, item in list(runs.items()):
        if item["source_label"] != source_token:
            continue
        run_summary = dict(item["summary"])
        run_summary.update(solve_stage)
        item["status"] = solve_stage["status"]
        item["artifact_path"] = solve_stage["artifact_path"]
        item["summary"] = run_summary
        runs[run_id] = item
        summary["runs"] = runs
        summary["artifact_verification_id"] = verification_id
        break


def run_verification_job(
    self,
    problem: str,
    username: str,
    commit: str | None = None,
    ref: str | None = None,
    *,
    sample_only: bool = False,
    verification_id: str = "",
) -> str:
    ctx = self.workspace_service.workspace_context(problem, username, include_recent=False)
    workspace = Path(ctx["workspace"]["path"])
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    source_commit = ""
    workspace_branch = cast(str | None, ctx["workspace"].get("branch")) or ""
    source_ref = ref if ref else commit if commit else workspace_branch if workspace_branch else "main"
    resolved_commit_override = ""
    generation_params_digest = ""
    toolchain_cmd_digest = self._toolchain_cmd_digest() or "unknown"
    use_verification_result_cache = False
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
                workspace_head = cast(str | None, status.get("head_commit")) or ""
                workspace_branch = cast(str | None, status.get("branch")) or ""
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

    target_verification_id = verification_id
    persist_into_existing_verification = bool(target_verification_id)

    cache_source_commit = source_commit
    cache_source_ref = source_ref
    cache_generation_params_digest = generation_params_digest
    cache_toolchain_cmd_digest = toolchain_cmd_digest
    if source_commit and generation_params_digest and (not persist_into_existing_verification):
        use_verification_result_cache = True
        cache_key = verification_cache_key(
            schema=self.VERIFICATION_CACHE_SCHEMA,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=cache_source_commit,
            source_ref=cache_source_ref,
            generation_params_digest=cache_generation_params_digest,
            toolchain_cmd_digest=cache_toolchain_cmd_digest,
            sample_only=bool(sample_only),
        )
        cached_verification_id = self._cached_verification_id_for_source(
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=cache_source_commit,
            source_ref=cache_source_ref,
            generation_params_digest=cache_generation_params_digest,
            toolchain_cmd_digest=cache_toolchain_cmd_digest,
            sample_only=bool(sample_only),
        )
        if cached_verification_id:
            return cached_verification_id
        cache_key_hash = verification_cache_key_hash(cache_key)

    artifact_ref_key = (
        cache_key
        if cache_key is not None
        else verification_cache_key(
            schema=self.VERIFICATION_CACHE_SCHEMA,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=cache_source_commit,
            source_ref=cache_source_ref,
            generation_params_digest=cache_generation_params_digest,
            toolchain_cmd_digest=cache_toolchain_cmd_digest,
            sample_only=bool(sample_only),
        )
    )
    artifact_ref = artifact_ref_from_cache_key_hash(
        self.fs_manager,
        schema=self.VERIFICATION_CACHE_SCHEMA,
        cache_key_hash=verification_cache_key_hash(artifact_ref_key),
    )
    verification_id = target_verification_id or f"ver-{uuid.uuid4().hex[:10]}"
    if cache_key is not None:
        existing_verification_id = ""
        with self._verification_inflight_lock:
            existing_verification_id = self._verification_inflight.get(cache_key_hash, "")
            if not existing_verification_id:
                self._verification_inflight[cache_key_hash] = verification_id
                inflight_owner = True
        if existing_verification_id:
            status = wait_build_terminal_status(
                self.db,
                verification_id=existing_verification_id,
                timeout_sec=self.VERIFICATION_JOIN_WAIT_TIMEOUT_SEC,
                poll_sec=self.VERIFICATION_JOIN_POLL_SEC,
            )
            if status == "ok":
                return existing_verification_id
            if status in {"failed", "cancelled"}:
                raise RuntimeError("same-configuration verification already failed; check logs and retry")
            raise RuntimeError("same-configuration verification is still running; refresh later")

    artifact_paths = ensure_artifact_paths(self.fs_manager, artifact_ref=artifact_ref)
    # Artifact refs are content-addressed and can be reused across retries. Ensure
    # each verification starts from a clean artifact layout to avoid stale files from a
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
    existing_summary: dict[str, object] = {}
    existing_status = "running"
    with verification_update_lock(verification_id):
        if persist_into_existing_verification:
            existing_record = load_verification_record(self.db, verification_id)
            if existing_record is not None:
                existing_summary = load_verification_summary(self.db, verification_id)
                existing_status = existing_record["status"]
        summary_seed = dict(existing_summary)
        summary_seed["verification_id"] = verification_id
        create_verification_record(
            self.db,
            self.fs_manager,
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=source_commit,
            source_ref=source_ref,
            kind=Kind.VERIFICATION,
            status=existing_status if persist_into_existing_verification else "running",
            summary=summary_seed,
            artifact_path=artifact_paths.root,
        )
        save_verification_summary(
            self.db,
            verification_id=verification_id,
            status=existing_status if persist_into_existing_verification else "running",
            summary=summary_seed,
            finished=False,
        )

    steps: list[dict] = []
    toolchain_digest = "unknown"
    seed = random.randint(1, 10**9)
    random.seed(seed)
    diagnostics: list[dict] = []
    build_cfg: dict = {}
    tests_spec_entries: list[dict] | None = None
    tests_spec_runtime: list[TestsSpecRuntimeRow] = []
    custom_sample_rows_by_test: dict[str, CustomSampleRow] = {}
    current_step = "compile"
    failing_test: str | None = None
    snapshot: Path | None = None
    final_status = "running"
    stage_results: dict[str, dict[str, object]] = {
        "generate_input": {
            "verification_source": "verification.generate-input",
            "status": "pending",
            "tests": [],
        },
        "solve_main": {
            "verification_source": "verification.solve-main",
            "status": "pending",
            "source": "",
            "artifact_path": "",
            "tests": [],
        },
    }

    try:
        if commit:
            source_commit = resolved_commit_override or self.workspace_service.resolve_commit(workspace, commit)
            source_ref = ref or commit
            self._verification_store.update_source_identity(
                verification_id,
                source_commit=source_commit,
                source_ref=source_ref,
            )
            snapshot = self.workspace_service.create_snapshot(workspace, source_commit)
        else:
            with self.workspace_service.workspace_lock(workspace):
                status = self.workspace_service.read_workspace_status(workspace)
                source_commit = cast(str | None, status.get("head_commit")) or ""
                branch = cast(str | None, status.get("branch")) or ""
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
                self._verification_store.update_source_identity(
                    verification_id,
                    source_commit=source_commit,
                    source_ref=source_ref,
                )
                snapshot = self.workspace_service.create_snapshot(
                    workspace,
                    None,
                    workspace_head=source_commit,
                    workspace_dirty=dirty,
                )

        build_cfg = self._load_build_config(snapshot)
        runtime_cfg = self._load_problem_runtime_config(snapshot)
        problem_mode = normalize_problem_mode(runtime_cfg.get("mode"), "pass-fail")
        problem_pass_limit = int(runtime_cfg.get("pass_limit", 1))
        interactive_mode = problem_mode == "interactive"
        time_limit_ms = int(runtime_cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
        run_timeout_ms = self._effective_run_timeout_ms(time_limit_ms, mode=problem_mode, pass_limit=problem_pass_limit)
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
            configured_generators = cast(list[str], build_cfg.get("generator_sources", []))
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

        accepted_rel = cast(str | None, build_cfg.get("accepted_solution_source")) or ""
        if not accepted_rel:
            accepted_rel = _default_accepted_solution_source(snapshot)
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
            name: source
            for name, source, _output in generator_targets
            if source is not None
        }
        compile_source_by_name: dict[str, Path] = {
            name: source
            for name, source, _output in compile_targets
            if source is not None
        }
        shared_testlib_blob: bytes | None = None
        resolved_testlib = workspace_testlib_header(snapshot)
        if resolved_testlib is not None:
            shared_testlib_blob = resolved_testlib.read_bytes()

        compile_jobs = 0
        compile_backend = self.judgehost_task_service
        try:
            if (not compile_backend.enabled()) or (not compile_backend.auth_token_configured()):
                raise RuntimeError("judge backend unavailable for verification compile")
        except Exception as exc:
            raise RuntimeError("judge backend unavailable for verification compile") from exc

        def _first_compile_message(summary: JudgehostRunSummary) -> str:
            diagnostics = summary.get("compile_diagnostics", [])
            for item in diagnostics:
                message = item.get("message", "")
                if message:
                    return message
            return summary.get("error", "")

        def _run_summary_task_id(summary: JudgehostRunSummary) -> str:
            judgehost = summary.get("judgehost")
            if judgehost is None:
                return ""
            return judgehost.get("task_id", "")

        def _run_summary_work_root(summary: JudgehostRunSummary) -> Path | None:
            task_id = _run_summary_task_id(summary)
            if not task_id:
                return None
            try:
                return compile_backend.domjudge_work_root_for_task(task_id)
            except Exception:
                return None

        def _run_summary_case_output(summary: JudgehostRunSummary, test_name: str) -> tuple[str, Path | None, int]:
            task_id = _run_summary_task_id(summary)
            if (not task_id) or (not test_name):
                return ("", None, 0)
            try:
                return compile_backend.domjudge_case_output_for_task(task_id, test_name)
            except Exception:
                return ("", None, 0)

        def _run_summary_test_result_map(summary: JudgehostRunSummary) -> dict[str, dict[str, str]]:
            tests = summary.get("tests", [])
            result_map: dict[str, dict[str, str]] = {}
            for row in tests:
                test_name = row.get("test", "")
                if not test_name:
                    continue
                pass_rows = row.get("passes", [])
                first_pass: TestPassSummary = pass_rows[0] if pass_rows else {}
                final_pass: TestPassSummary | None = None
                for item in pass_rows:
                    verdict_token = item.get("verdict", "").upper()
                    if verdict_token and verdict_token != "-":
                        final_pass = item
                if final_pass is None:
                    final_pass = first_pass
                verdict = row.get("verdict") or final_pass.get("verdict", "") or first_pass.get("verdict", "")
                verdict = verdict.upper()
                feedback = final_pass.get("feedback", "") or first_pass.get("feedback", "") or row.get("feedback", "")
                output_ref = ""
                for key in ("output_ref", "output_artifact", "output_rel"):
                    token = final_pass.get(key, "") or first_pass.get(key, "") or row.get(key, "")
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
            tests_payload: list[NormalizedTestPayload],
            extra_sources_b64: dict[str, str] | None = None,
            manual_validate_only: bool = False,
        ) -> dict[str, tuple[int, str]]:
            owner_verification_id = verification_id
            safe_source_name = Path(source_name if source_name else "submission.cpp").name
            normalized_tests: list[NormalizedTestPayload] = []
            for row in tests_payload:
                test_name = Path(row["name"]).name
                if not RUN_TEST_NAME_RE.fullmatch(test_name):
                    continue
                normalized_tests.append(
                    {
                        "name": test_name,
                        "input_b64": row["input_b64"],
                        "answer_name": row["answer_name"],
                        "answer_b64": row["answer_b64"],
                    }
                )
            if not normalized_tests:
                return {}
            run_id = f"r-bg-{uuid.uuid4().hex[:12]}"
            validator_source = compile_source_by_name.get("validator")
            sources_payload: dict[str, str] = {}
            binaries_payload: dict[str, str] = {}
            submission_extra_sources_payload: dict[str, str] = {}

            if validator_source is not None and validator_source.exists() and validator_source.is_file():
                sources_payload["validator.cpp"] = base64.b64encode(validator_source.read_bytes()).decode("ascii")
                if shared_testlib_blob is not None:
                    sources_payload["testlib.h"] = base64.b64encode(shared_testlib_blob).decode("ascii")
            if extra_sources_b64 is not None:
                for raw_name, raw_blob in extra_sources_b64.items():
                    safe_name = Path(raw_name).name
                    if not safe_name:
                        continue
                    submission_extra_sources_payload[safe_name] = raw_blob
                    if safe_name in sources_payload:
                        continue
                    sources_payload[safe_name] = raw_blob

            checker_args: list[str] = []
            if manual_validate_only:
                checker_args.append("--validate-input")

            prepared_payload: dict[str, object] = {
                "verification_payload": {
                    "tests": normalized_tests,
                    "run_config_json": json.dumps(
                        {
                            "checker_mode": "testlib",
                            "checker_args": checker_args,
                            "pass_limit": int(runtime_cfg.get("pass_limit", 1)),
                            "time_limit_ms": 30000,
                            "memory_limit_mb": int(runtime_cfg.get("memory_limit_mb", 1024)),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "problem_limits": {
                        "time_limit_ms": 30000,
                        "memory_limit_mb": int(runtime_cfg.get("memory_limit_mb", 1024)),
                        "pass_limit": int(runtime_cfg.get("pass_limit", 1)),
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
                artifact_verification_id=owner_verification_id,
                mode=problem_mode,
                submission_path=None,
                upload_content=source_bytes,
                upload_filename=safe_source_name,
                run_id=run_id,
                selected_tests=[],
                verification_id=owner_verification_id,
                verification_run_ids=[run_id],
                expected_behavior="accepted",
                verification_source="verification.generate-input",
                task_kind="generate",
                compile_only=False,
                persist_verification_run=False,
                prepared_payload=prepared_payload,
            )
            task_result = cast(JudgehostTaskResult, compile_backend.wait_for_task_result(task_id, timeout_sec=None))
            task_status = task_result.get("task_status", "")
            if task_status == compile_backend.STATUS_FAILED:
                task_error = task_result.get("error", "") or "judge backend generate task failed"
                return {row["name"]: (1, task_error) for row in normalized_tests}
            run_status = task_result.get("status", "")
            summary_obj = task_result.get("summary", {})
            if run_status and run_status != "ok":
                summary_error = summary_obj.get("error", "") or f"judge backend run status is {run_status}"
                return {row["name"]: (1, summary_error) for row in normalized_tests}
            artifact_path = task_result.get("artifact_path", "")
            run_root = Path(artifact_path).resolve() if artifact_path else Path()
            work_root_hint = _run_summary_work_root(summary_obj)
            test_result_map = _run_summary_test_result_map(summary_obj)
            results: dict[str, tuple[int, str]] = {}
            for row in normalized_tests:
                case_name = row["name"]
                case_result = test_result_map.get(case_name, {})
                verdict = case_result.get("verdict", "")
                if verdict and verdict != "OK":
                    detail = case_result.get("feedback", "")
                    if (not detail) and verdict == "CE":
                        detail = _first_compile_message(summary_obj)
                    if not detail:
                        detail = summary_obj.get("error", "")
                    if not detail:
                        detail = f"judge verdict {verdict}"
                    results[case_name] = (1, detail)
                    continue
                if manual_validate_only:
                    results[case_name] = (0, "")
                    continue
                output_ref = case_result.get("output_ref", "")
                case_output_ref, case_work_root, case_id = _run_summary_case_output(summary_obj, case_name)
                if not output_ref:
                    output_ref = case_output_ref
                blob_work_root = case_work_root or work_root_hint
                output_blob: bytes | None = None
                if output_ref:
                    try:
                        output_blob = compile_backend.resolve_artifact_blob(output_ref, work_root=blob_work_root)
                    except Exception:
                        output_blob = None
                if output_blob is None:
                    fallback_candidates = [(run_root / f"{Path(case_name).stem}.out").resolve()]
                    if output_ref and (not output_ref.startswith("cache://")):
                        fallback_root = blob_work_root or run_root
                        fallback_candidates.append((fallback_root / output_ref).resolve())
                    if (case_id > 0) and (case_work_root is not None):
                        fallback_candidates.append((case_work_root / "results" / f"{case_id}" / "program.out").resolve())
                    for fallback in fallback_candidates:
                        if fallback.exists() and fallback.is_file() and (not fallback.is_symlink()):
                            output_blob = fallback.read_bytes()
                            break
                if output_blob is None:
                    detail = case_result.get("feedback", "")
                    if not detail:
                        detail = summary_obj.get("error", "")
                    if not detail:
                        detail = "judge backend did not produce generated input output"
                    results[case_name] = (1, detail)
                    continue
                results[case_name] = (0, output_blob.decode("utf-8", errors="replace"))
            return results

        with (logs_dir / "compile.log").open("w", encoding="utf-8") as clog:
            clog.write("compile_jobs=0\n")
            clog.write("compile_strategy=judgehost-source-only\n")
            for name, source, _output in compile_targets:
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
        tests_meta: list[dict[str, object]] = []
        source_answer_by_test: dict[str, Path] = {}
        generate_stage_tests: list[dict[str, object]] = []
        counter = 1
        manual_count = 0
        generated_count = 0
        with (logs_dir / "generate.log").open("w", encoding="utf-8") as glog:
            planned_rows: list[PlannedRow] = []
            if tests_spec_entries is not None:
                glog.write("tests_source=tests/spec.json\n")
                for row in tests_spec_runtime:
                    test_id = row["id"]
                    is_sample = row["sample"]
                    if sample_only and (not is_sample):
                        continue
                    custom_sample_input = row["sample_input"]
                    custom_sample_output = row["sample_output"]
                    custom_sample_output_validate = row["sample_output_validate"]
                    file_index = row["index"] if sample_only else counter
                    dst = artifact_paths.tests / f"{file_index:03d}.in"
                    if row["kind"] == "manual":
                        planned_rows.append(
                            {
                                "kind": "manual",
                                "dst": dst,
                                "input_bytes": row["input"].encode("utf-8"),
                                "tests_meta": {
                                    "index": file_index,
                                    "kind": "manual",
                                    "id": test_id,
                                    "sample": is_sample,
                                    "sample_input_custom": bool(custom_sample_input),
                                    "sample_output_custom": bool(custom_sample_output),
                                    "sample_output_validate": custom_sample_output_validate,
                                    "desc": f"manual {test_id}" if test_id else "manual",
                                    "source": row["source_rel"],
                                },
                                "log_prefix": f"manual id={test_id} index={row['index']}",
                                "error_context": f"tests/spec.json entry {row['index']} (id={test_id})",
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

                    gen_source = generator_source_by_name.get(row["target_name"])
                    if gen_source is None:
                        raise RuntimeError(
                            f"generator source is required for tests/spec.json entry {row['index']}"
                        )
                    command_payload = (
                        " ".join(['"$SUBMISSION_BIN"', *[shlex.quote(item) for item in row["args"]]])
                        if row["args"]
                        else '"$SUBMISSION_BIN"'
                    )
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
                                "sample_output_validate": custom_sample_output_validate,
                                "desc": row["cmd"] if row["cmd"] else "gen",
                                "command": row["cmd"],
                                "source": row["source_rel"],
                                "payload_source": row["payload_rel"],
                            },
                            "log_prefix": f"gen id={test_id} index={row['index']} source={row['source_rel']} cmd={row['cmd']}",
                            "error_context": f"tests/spec.json entry {row['index']} (id={test_id})",
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
                        source_rel = t.relative_to(snapshot).as_posix()
                    except ValueError:
                        source_rel = t.name
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
                for gen_index, (_name, source, _target) in enumerate(generator_targets, start=1):
                    if source is None:
                        continue
                    try:
                        source_label = source.relative_to(snapshot).as_posix()
                    except ValueError:
                        source_label = source.as_posix()
                    generator_execs.append((gen_index, source_label, source))

                if generator_execs:
                    runs = int(build_cfg.get("generator_runs", 3))
                    generator_args = cast(list[str], build_cfg.get("generator_args", []))
                    for gen_index, source_label, gen_source in generator_execs:
                        for i in range(runs):
                            dst = artifact_paths.tests / f"{counter:03d}.in"
                            desc = f"gen: {source_label}"
                            if generator_args:
                                desc = f"{desc} {' '.join(generator_args)}"
                            command_payload = (
                                " ".join(['"$SUBMISSION_BIN"', *[shlex.quote(item) for item in generator_args]])
                                if generator_args
                                else '"$SUBMISSION_BIN"'
                            )
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

            manual_batch_payload: list[NormalizedTestPayload] = []
            gen_batch_payloads_by_source: dict[Path, list[NormalizedTestPayload]] = {}
            for planned in planned_rows:
                dst = planned["dst"]
                if planned["kind"] == "manual":
                    manual_batch_payload.append(
                        {
                            "name": dst.name,
                            "input_b64": base64.b64encode(planned["input_bytes"]).decode("ascii"),
                            "answer_name": f"{dst.stem}.ans",
                            "answer_b64": "",
                        }
                    )
                    continue
                gen_source = planned["generator_source"]
                payload: NormalizedTestPayload = {
                    "name": dst.name,
                    "input_b64": base64.b64encode((planned["command_payload"] + "\n").encode("utf-8")).decode("ascii"),
                    "answer_name": f"{dst.stem}.ans",
                    "answer_b64": "",
                }
                if gen_source in gen_batch_payloads_by_source:
                    gen_batch_payloads_by_source[gen_source].append(payload)
                else:
                    gen_batch_payloads_by_source[gen_source] = [payload]
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
                kind = planned["kind"]
                rc, output_or_err = (manual_results_by_name if kind == "manual" else gen_results_by_name).get(dst.name, (1, "judge backend generate result missing"))
                glog.write(f"{planned['log_prefix']} -> {dst.name} rc={rc}\n")
                if rc != 0:
                    if output_or_err:
                        glog.write(output_or_err + "\n")
                    generate_stage_tests.append(
                        {
                            "test": dst.name,
                            "verdict": "FL",
                            "message": output_or_err,
                            "source_kind": kind,
                        }
                    )
                    stage_results["generate_input"] = {
                        "verification_source": "verification.generate-input",
                        "status": "failed",
                        "tests": list(generate_stage_tests),
                        "manual_count": manual_count,
                        "generated_count": generated_count,
                        "total": len(generate_stage_tests),
                    }
                    dst.unlink(missing_ok=True)
                    failing_test = dst.name
                    failure = "validator failed on" if kind == "manual" else "generator failed on"
                    raise RuntimeError(f"{failure} {planned['error_context']}: {output_or_err}")
                if kind == "manual":
                    dst.write_bytes(planned["input_bytes"])
                    manual_count += 1
                else:
                    dst.write_text(output_or_err, encoding="utf-8")
                    generated_count += 1
                generate_stage_tests.append(
                    {
                        "test": dst.name,
                        "verdict": "OK",
                        "message": "",
                        "source_kind": kind,
                    }
                )
                test_files.append(dst)
                tests_meta.append(planned["tests_meta"])
                custom_sample_row = planned["custom_sample_row"]
                if custom_sample_row is not None:
                    custom_sample_rows_by_test[dst.name] = custom_sample_row
                answer_source = planned["answer_source"]
                if answer_source is not None:
                    source_answer_by_test[dst.name] = answer_source
            glog.write(f"manual_tests={manual_count}\n")
            glog.write(f"generated_tests={generated_count}\n")
            glog.write(f"total_tests={len(test_files)}\n")
        stage_results["generate_input"] = {
            "verification_source": "verification.generate-input",
            "status": "ok",
            "tests": list(generate_stage_tests),
            "manual_count": manual_count,
            "generated_count": generated_count,
            "total": len(generate_stage_tests),
        }
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
            vlog.write("input validation is recorded in build summary stage_results.generate_input\n")
            vlog.write(f"validated_tests={len(test_files)}\n")
        steps.append({"step": "validate", "status": "ok", "log": "logs/validate.log"})

        current_step = "solve"
        solve_jobs = self._effective_compile_jobs(build_cfg.get("solve_jobs", 0), len(test_files))
        custom_sample_output_validate_total = 0
        custom_sample_output_validate_checked = 0
        solve_results: dict[str, SolveResultRow] = {}
        solve_stage_result: SolveStageResultOut = {}
        solve_backend = self.judgehost_task_service.backend_name()
        with (logs_dir / "solve.log").open("w", encoding="utf-8") as slog:
            slog.write(f"solve_jobs={solve_jobs}\n")
            slog.write(f"solve_backend={solve_backend}\n")
            slog.write(f"build_solve_timeout_sec={build_solve_timeout_sec}\n")

            def _solve_failure_message(test_name: str, row: SolveResultRow) -> str:
                def _main_status_token(result_row: SolveResultRow) -> str:
                    verdict = result_row["verdict"]
                    if verdict == "AC":
                        return "AC"
                    if verdict == "TL":
                        return "TL"
                    if verdict == "WA":
                        return "WA"
                    if verdict == "RE":
                        return "RE"
                    if verdict == "CE":
                        return "CE"
                    if verdict == "FL":
                        return "FL"
                    if result_row["timed_out"]:
                        return "TL"
                    if result_row["rc"] != 0:
                        return "FL"
                    return ""

                rc = row["rc"]
                worker_error = row["worker_error"]
                timed_out = row["timed_out"]
                stderr_text = compact_single_line(row["stderr"], 220)
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

            def _update_solve_stage(status_token: str) -> None:
                solve_stage_summary = dict(solve_stage_result.get("summary") or {})
                tests_payload = canonicalize_verification_test_rows(list(solve_stage_summary.get("tests") or []))
                if not tests_payload:
                    for test_name, result_row in solve_results.items():
                        verdict = result_row["verdict"]
                        if verdict == "AC":
                            verdict = "OK"
                        if not verdict:
                            verdict = "OK" if result_row["rc"] == 0 else "FL"
                        message = result_row["worker_error"] if result_row["worker_error"] else result_row["stderr"]
                        tests_payload.append(
                            build_verification_test_row(
                                test_name=test_name,
                                verdict=verdict,
                                message=message,
                            )
                        )
                solve_artifact_path = solve_stage_result["artifact_path"] if "artifact_path" in solve_stage_result else ""
                if solve_stage_summary:
                    solve_stage_summary["tests"] = tests_payload
                    solve_stage_summary["verification_source"] = "verification.solve-main"
                    solve_stage_summary["status"] = status_token
                    solve_stage_summary["source"] = accepted_rel
                    solve_stage_summary["artifact_path"] = solve_artifact_path
                    if not solve_stage_summary.get("mode"):
                        solve_stage_summary["mode"] = problem_mode
                    if not solve_stage_summary.get("tests_total"):
                        solve_stage_summary["tests_total"] = len(test_files)
                    stage_results["solve_main"] = cast(SolveStageSummary, solve_stage_summary)
                    return
                stage_results["solve_main"] = {
                    "verification_source": "verification.solve-main",
                    "status": status_token,
                    "source": accepted_rel,
                    "artifact_path": solve_artifact_path,
                    "tests_total": len(test_files),
                    "tests": tests_payload,
                }

            solve_results = cast(
                dict[str, SolveResultRow],
                solve_with_judge_backend(
                    self,
                    problem=problem,
                    username=username,
                    artifact_verification_id=verification_id,
                    accepted_source_rel=accepted_rel,
                    mode=problem_mode,
                    test_files=test_files,
                    ans_dir=artifact_paths.ans,
                    solve_jobs=solve_jobs,
                    source_answer_by_test=source_answer_by_test,
                    stage_result_out=solve_stage_result,
                ),
            )
            pending_solve_failure = ""
            for t in test_files:
                if t.name in solve_results:
                    row = solve_results[t.name]
                else:
                    row = cast(SolveResultRow, solve_result_error("missing judge solve result"))
                    solve_results[t.name] = row
                rc = row["rc"]
                timed_out = row["timed_out"]
                err = row["worker_error"] if row["worker_error"] else row["stderr"]
                slog.write(
                    f"{t.name}: rc={rc}{' timed_out=1' if timed_out else ''}\n{err}\n"
                )
                fail_msg = _solve_failure_message(t.name, row)
                if fail_msg:
                    failing_test = t.name
                    slog.write(f"early_stop={t.name}\n")
                    pending_solve_failure = fail_msg
                    break
            _update_solve_stage("failed" if pending_solve_failure else "ok")
            if pending_solve_failure:
                raise RuntimeError(pending_solve_failure)

            if custom_sample_rows_by_test:
                with (logs_dir / "sample_output_validate.log").open("w", encoding="utf-8") as cvlog:
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
            "artifact_ref": artifact_ref,
            "solve_backend": solve_backend,
            "time_limit_ms": time_limit_ms,
            "run_timeout_ms": run_timeout_ms,
            "run_timeout_sec": run_timeout_sec,
            "generator_sources": cast(list[str], build_cfg.get("generator_sources", [])),
            "generator_args": cast(list[str], build_cfg.get("generator_args", [])),
            "validator_args": cast(list[str], build_cfg.get("validator_args", [])),
            "checker_args": cast(list[str], build_cfg.get("checker_args", [])),
            "checker_standard": cast(str, build_cfg.get("checker_standard", "")),
            "pass_limit": problem_pass_limit,
            "sandbox_backend": self.execution_backend_name,
            "sandbox_memory_mb": self.default_exec_memory_mb,
            "sandbox_process_limit": self.default_exec_process_limit,
            "sandbox_output_kb": self.default_exec_output_kb,
            "generation_params_digest": cache_generation_params_digest,
            "toolchain_cmd_digest": cache_toolchain_cmd_digest,
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

        persisted_summary_obj = {
            "artifact_ref": artifact_ref,
            "verification_id": verification_id,
            "steps": steps,
            "diagnostics": diagnostics,
            "generation_params": generation_params,
            "stage_results": stage_results,
        }
        persisted_summary = summary_for_db(
            persisted_summary_obj,
            tests_limit=self.DB_SUMMARY_TESTS_LIMIT,
            diagnostics_limit=self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            feedback_files_limit=self.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
            diagnostic_message_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
        )
        if persist_into_existing_verification:
            with verification_update_lock(verification_id):
                merged_summary = load_verification_summary(self.db, verification_id)
                merged_summary.update(persisted_summary_obj)
                merged_summary["verification_id"] = verification_id
                _promote_solve_main_stage_into_runs(merged_summary, verification_id=verification_id)
                current_record = load_verification_record(self.db, verification_id)
                save_verification_summary(
                    self.db,
                    verification_id=verification_id,
                    status=current_record["status"] if current_record is not None else Status.RUNNING.value,
                    summary=merged_summary,
                    finished=False,
                )
        else:
            self._verification_store.save_summary_record(
                verification_id=verification_id,
                status="ok",
                summary_json=persisted_summary,
                finished=True,
            )
        if use_verification_result_cache and self._async_task_cache_service is not None and cache_source_commit:
            self._async_task_cache_service.put(
                self.VERIFICATION_CACHE_NAMESPACE,
                cache_key
                if cache_key is not None
                else verification_cache_key(
                    schema=self.VERIFICATION_CACHE_SCHEMA,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    source_commit=cache_source_commit,
                    source_ref=cache_source_ref,
                    generation_params_digest=cache_generation_params_digest,
                    toolchain_cmd_digest=cache_toolchain_cmd_digest,
                    sample_only=bool(sample_only),
                ),
                {"verification_id": verification_id},
                tags={
                    "problem_id": str(problem_id),
                    "workspace_id": str(workspace_id),
                    "source_commit": cache_source_commit,
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
        persisted_summary_obj = {
            "artifact_ref": artifact_ref,
            "verification_id": verification_id,
            "error": str(exc),
            "failed_step": current_step,
            "failed_test": failing_test,
            "steps": steps,
            "diagnostics": diagnostics,
            "stage_results": stage_results,
        }
        persisted_summary = summary_for_db(
            persisted_summary_obj,
            tests_limit=self.DB_SUMMARY_TESTS_LIMIT,
            diagnostics_limit=self.DB_SUMMARY_DIAGNOSTICS_LIMIT,
            feedback_files_limit=self.DB_SUMMARY_FEEDBACK_FILES_LIMIT,
            diagnostic_message_limit=self.DB_SUMMARY_DIAGNOSTIC_MESSAGE_LIMIT,
        )
        if persist_into_existing_verification:
            with verification_update_lock(verification_id):
                merged_summary = load_verification_summary(self.db, verification_id)
                merged_summary.update(persisted_summary_obj)
                merged_summary["verification_id"] = verification_id
                _promote_solve_main_stage_into_runs(merged_summary, verification_id=verification_id)
                save_verification_summary(
                    self.db,
                    verification_id=verification_id,
                    status="failed",
                    summary=merged_summary,
                    finished=False,
                )
        else:
            self._verification_store.save_summary_record(
                verification_id=verification_id,
                status="failed",
                summary_json=persisted_summary,
                finished=True,
            )
        final_status = "failed"
    finally:
        if final_status != "running":
            self.workspace_service._store.set_recent_verification_status(int(workspace_id), final_status)
        if snapshot is not None:
            shutil.rmtree(snapshot.parent, ignore_errors=True)
        if inflight_owner and cache_key_hash:
            with self._verification_inflight_lock:
                current = self._verification_inflight.get(cache_key_hash, "")
                if current == verification_id:
                    self._verification_inflight.pop(cache_key_hash, None)

    return verification_id
