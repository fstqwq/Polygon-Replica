from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.service.verification import load_verification_summary

RUN_TEST_NAME_RE = re.compile(r"^[0-9]{3}\.in$")


def _normalize_verdict_token(raw: object) -> str:
    token = str(raw or "").strip().upper()
    if token in {"OK", "AC", "ACCEPTED", "CORRECT"}:
        return "AC"
    if token.startswith("TL"):
        return "TL"
    if token in {"WA", "WRONG-ANSWER", "WRONG_ANSWER"}:
        return "WA"
    if token in {"RE", "RUN-ERROR", "RUN_ERROR", "RUNTIME-ERROR", "RUNTIME_ERROR"}:
        return "RE"
    if token in {"CE", "COMPILER-ERROR", "COMPILER_ERROR"}:
        return "CE"
    if token in {"FL", "FAIL", "FAILED", "INTERNAL-ERROR", "INTERNAL_ERROR", "COMPARE-ERROR", "COMPARE_ERROR"}:
        return "FL"
    return ""


def solve_with_judge_backend(
    self,
    *,
    problem: str,
    username: str,
    artifact_verification_id: str,
    accepted_source_rel: str,
    mode: str,
    test_files: list[Path],
    ans_dir: Path,
    solve_jobs: int = 1,
    source_answer_by_test: dict[str, Path] | None = None,
    stage_result_out: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    service = self.judgehost_task_service
    selected_tests = [str(p.name) for p in test_files if RUN_TEST_NAME_RE.fullmatch(str(p.name))]
    if not selected_tests:
        return {}
    solve_mode = str(mode or "pass-fail").strip().lower()
    if solve_mode not in {"pass-fail", "interactive", "multi-pass"}:
        solve_mode = "pass-fail"
    requested_parallelism = max(1, int(solve_jobs))
    effective_parallelism = max(1, min(requested_parallelism, len(selected_tests)))
    try:
        status = service.status()
    except Exception:
        status = {}
    if isinstance(status, dict):
        try:
            hosts_online = max(0, int(status.get("hosts_online") or 0))
        except Exception:
            hosts_online = 0
        try:
            hosts_total = max(0, int(status.get("hosts_total") or 0))
        except Exception:
            hosts_total = 0
        host_count = hosts_online if hosts_online > 0 else hosts_total
        try:
            fetch_batch_size = max(1, int(status.get("fetch_batch_size") or 1))
        except Exception:
            fetch_batch_size = 1
        if host_count > 0:
            effective_parallelism = max(
                1,
                min(effective_parallelism, host_count * fetch_batch_size),
            )
    # Keep one stable judgehost job per accepted source during verification.solve-main.
    # This guarantees one compile per source and enables deterministic
    # per-test cache hydration keyed by a stable work root.
    effective_parallelism = 1

    solve_results: dict[str, dict[str, object]] = {}

    def _split_chunks(items: list[str], chunk_count: int) -> list[list[str]]:
        total = len(items)
        if total <= 0:
            return []
        count = max(1, min(chunk_count, total))
        base = total // count
        extra = total % count
        cursor = 0
        out: list[list[str]] = []
        for idx in range(count):
            size = base + (1 if idx < extra else 0)
            chunk = items[cursor : cursor + size]
            cursor += size
            if chunk:
                out.append(chunk)
        return out

    verification_id = f"ver-solve-main-{artifact_verification_id}-{uuid.uuid4().hex[:8]}"
    target_run_id = ""
    verification_summary = load_verification_summary(self.db, artifact_verification_id)
    if isinstance(verification_summary, dict):
        runs_obj = verification_summary.get("runs")
        runs = runs_obj if isinstance(runs_obj, dict) else {}
        for candidate_run_id, item in runs.items():
            if not isinstance(item, dict):
                continue
            source_label = str(item.get("source_label") or "").strip()
            if source_label == accepted_source_rel:
                target_run_id = str(candidate_run_id or "").strip()
                if target_run_id:
                    break
    plans: list[dict[str, object]] = []
    for idx, chunk in enumerate(_split_chunks(list(selected_tests), effective_parallelism)):
        plans.append(
            {
                "index": idx,
                "run_id": f"r-solve-main-{uuid.uuid4().hex[:12]}",
                "tests": chunk,
            }
        )
    if not plans:
        return solve_results
    verification_run_ids = [
        str(plan.get("run_id") or "").strip()
        for plan in plans
        if str(plan.get("run_id") or "").strip()
    ]

    def _submit_and_wait_chunk(plan: dict[str, object]) -> dict[str, object]:
        chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
        run_id = str(plan.get("run_id") or "").strip() or f"r-solve-main-{uuid.uuid4().hex[:12]}"
        task_id = service.enqueue_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=solve_mode,
            submission_path=accepted_source_rel,
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=chunk,
            verification_id=verification_id,
            verification_run_ids=list(verification_run_ids),
            expected_behavior="accepted",
            verification_source="verification.solve-main",
            task_kind="solve",
            persist_verification_run=False,
            prepared_payload={
                "verification_target_run_id": target_run_id,
            } if target_run_id else None,
        )
        result = service.wait_for_task_result(task_id, timeout_sec=None)
        if str(result.get("task_status") or "").strip().lower() == service.STATUS_FAILED:
            detail = str(result.get("error") or "").strip() or "judge backend task failed"
            raise RuntimeError(detail)
        if not str(result.get("run_id") or "").strip():
            result["run_id"] = run_id
        return result

    def _summary_work_root(summary_obj: dict[str, Any]) -> Path | None:
        judgehost_obj = summary_obj.get("judgehost")
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
        if job_row is None:
            return None
        work_root = str(job_row["work_root"] or "").strip()
        if not work_root:
            return None
        try:
            return Path(work_root).resolve()
        except Exception:
            return None

    def _consume_chunk_result(chunk: list[str], task_result: dict[str, object]) -> tuple[bool, str]:
        run_id = str(task_result.get("run_id") or "").strip()
        summary_obj = dict(task_result.get("summary") or {})
        artifact_path = str(task_result.get("artifact_path") or "").strip()
        run_status = str(task_result.get("status") or "").strip().lower()
        if isinstance(stage_result_out, dict):
            stage_result_out.clear()
            stage_result_out.update(
                {
                    "verification_source": "verification.solve-main",
                    "status": run_status or "ok",
                    "artifact_path": artifact_path,
                    "run_id": run_id,
                    "summary": dict(summary_obj),
                }
            )
        if (not run_id) and (not summary_obj):
            msg = "judge backend result missing"
            for name in chunk:
                solve_results[name] = self._solve_result_error(msg, verdict="FL")
            return (False, chunk[0] if chunk else "")
        run_root = Path(artifact_path).resolve() if artifact_path else Path()
        work_root_hint = _summary_work_root(summary_obj)
        tests_summary = summary_obj.get("tests")
        tests_rows = [row for row in tests_summary if isinstance(row, dict)] if isinstance(tests_summary, list) else []
        feedback_by_test: dict[str, str] = {}
        verdict_by_test: dict[str, str] = {}
        output_ref_by_test: dict[str, str] = {}
        for row in tests_rows:
            test_name = str(row.get("test") or "").strip()
            if not test_name:
                continue
            passes = row.get("passes")
            if not isinstance(passes, list) or (not passes):
                continue
            pass_rows = [item for item in passes if isinstance(item, dict)]
            first_pass = pass_rows[0] if pass_rows else {}
            verdict = str(row.get("verdict") or first_pass.get("verdict") or "").strip().upper()
            if verdict:
                verdict_by_test[test_name] = verdict
            final_pass_row: dict[str, Any] | None = None
            for item in pass_rows:
                token = str(item.get("verdict") or "").strip().upper()
                if token and token != "-":
                    final_pass_row = item
            if final_pass_row is None:
                final_pass_row = first_pass if isinstance(first_pass, dict) else {}
            feedback = str(final_pass_row.get("feedback") or first_pass.get("feedback") or "").strip()
            if feedback:
                feedback_by_test[test_name] = feedback
            for key in ("output_ref", "output_artifact", "output_rel"):
                token = str(final_pass_row.get(key) or row.get(key) or "").strip()
                if token:
                    output_ref_by_test[test_name] = token
                    break
        compile_detail = self._judge_backend_compile_detail(summary_obj, run_root)

        for idx, test_name in enumerate(chunk):
            if run_status and run_status != "ok":
                detail = feedback_by_test.get(test_name) or str(summary_obj.get("error") or "").strip()
                if (not detail) and compile_detail:
                    detail = compile_detail
                if not detail:
                    detail = f"judge backend run status is {run_status}"
                run_failed_verdict = _normalize_verdict_token(verdict_by_test.get(test_name)) or "FL"
                solve_results[test_name] = self._solve_result_error(detail, verdict=run_failed_verdict)
                for rest_name in chunk[idx + 1 :]:
                    if rest_name not in solve_results:
                        solve_results[rest_name] = self._solve_result_error(
                            f"skipped (prior test {test_name} failed)",
                            verdict=run_failed_verdict,
                        )
                return (False, test_name)
            verdict = verdict_by_test.get(test_name, "")
            if verdict and verdict != "OK":
                detail = feedback_by_test.get(test_name) or ""
                if (not detail) and verdict == "CE":
                    detail = compile_detail
                if not detail:
                    detail = f"judge verdict {verdict}"
                solve_results[test_name] = self._solve_result_error(
                    detail,
                    verdict=_normalize_verdict_token(verdict) or "FL",
                )
                for rest_name in chunk[idx + 1 :]:
                    if rest_name not in solve_results:
                        solve_results[rest_name] = self._solve_result_error(
                            f"skipped (prior test {test_name} failed)",
                            verdict=_normalize_verdict_token(verdict) or "FL",
                        )
                return (False, test_name)
            stem = Path(test_name).stem
            source_out = (run_root / f"{stem}.out").resolve()
            target_ans = (ans_dir / f"{stem}.ans").resolve()
            source_answer = source_answer_by_test.get(test_name) if isinstance(source_answer_by_test, dict) else None
            if isinstance(source_answer, Path):
                try:
                    if source_answer.exists() and source_answer.is_file() and (not source_answer.is_symlink()):
                        target_ans.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_answer, target_ans)
                        solve_results[test_name] = self._solve_result_ok()
                        continue
                except OSError:
                    pass
            output_ref = str(output_ref_by_test.get(test_name) or "").strip()
            if output_ref:
                output_blob: bytes | None = None
                judgehost = self.judgehost_task_service
                try:
                    output_blob = judgehost.resolve_artifact_blob(output_ref, work_root=work_root_hint)
                except Exception:
                    output_blob = None
                if (output_blob is None) and (not output_ref.startswith("cache://")):
                    source_ref = (run_root / output_ref).resolve()
                    if source_ref.exists() and source_ref.is_file() and (not source_ref.is_symlink()):
                        try:
                            output_blob = source_ref.read_bytes()
                        except OSError:
                            output_blob = None
                if output_blob is not None:
                    target_ans.parent.mkdir(parents=True, exist_ok=True)
                    target_ans.write_bytes(output_blob)
                    solve_results[test_name] = self._solve_result_ok()
                    continue
            if source_out.exists() and source_out.is_file():
                target_ans.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_out, target_ans)
                solve_results[test_name] = self._solve_result_ok()
                continue
            if verdict == "OK":
                target_ans.parent.mkdir(parents=True, exist_ok=True)
                target_ans.write_bytes(b"")
                solve_results[test_name] = self._solve_result_ok()
                continue
            detail = feedback_by_test.get(test_name) or str(summary_obj.get("error") or "").strip()
            if not detail:
                detail = f"judge backend did not produce output for {test_name}"
            missing_output_verdict = _normalize_verdict_token(verdict) or "FL"
            solve_results[test_name] = self._solve_result_error(detail, verdict=missing_output_verdict)
            for rest_name in chunk[idx + 1 :]:
                if rest_name not in solve_results:
                    solve_results[rest_name] = self._solve_result_error(
                        f"skipped (prior test {test_name} failed)",
                        verdict=missing_output_verdict,
                    )
            return (False, test_name)
        return (True, "")

    def _mark_remaining_chunks(start_index: int, failed_test_name: str) -> None:
        reason = (
            f"skipped (prior test {failed_test_name} failed)"
            if failed_test_name
            else "skipped (prior test failed)"
        )
        for idx in range(max(0, int(start_index)), len(plans)):
            chunk = [str(item or "").strip() for item in list(plans[idx].get("tests") or []) if str(item or "").strip()]
            for test_name in chunk:
                if test_name not in solve_results:
                    solve_results[test_name] = self._solve_result_error(reason, verdict="FL")

    def _mark_unresolved_tests(failed_test_name: str) -> None:
        reason = (
            f"skipped (prior test {failed_test_name} failed)"
            if failed_test_name
            else "skipped (prior test failed)"
        )
        for plan in plans:
            chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
            for test_name in chunk:
                if test_name not in solve_results:
                    solve_results[test_name] = self._solve_result_error(reason, verdict="FL")

    if len(plans) <= 1:
        plan = plans[0]
        chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
        task_result: dict[str, object] | None = None
        try:
            task_result = _submit_and_wait_chunk(plan)
        except Exception as exc:
            detail = str(exc or "").strip() or "judge backend task failed"
            for name in chunk:
                solve_results[name] = self._solve_result_error(detail, verdict="FL")
            _mark_remaining_chunks(1, chunk[0] if chunk else "")
            return solve_results
        ok, failed_test = _consume_chunk_result(chunk, task_result or {})
        if not ok:
            _mark_remaining_chunks(1, failed_test)
        return solve_results

    pool = ThreadPoolExecutor(max_workers=effective_parallelism)
    pool_shutdown = False
    try:
        inflight: dict[object, int] = {}
        next_submit_idx = 0

        def _submit_plan(idx: int) -> None:
            plan = plans[idx]
            inflight[pool.submit(_submit_and_wait_chunk, plan)] = idx

        while (next_submit_idx < len(plans)) and (len(inflight) < effective_parallelism):
            _submit_plan(next_submit_idx)
            next_submit_idx += 1

        while inflight:
            done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                idx = inflight.pop(future)
                plan = plans[idx]
                fallback_run_id = str(plan.get("run_id") or "").strip() or f"r-solve-main-{uuid.uuid4().hex[:12]}"
                try:
                    task_result = future.result()
                    chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
                    if not isinstance(task_result, dict):
                        task_result = {"run_id": fallback_run_id}
                    if not str(task_result.get("run_id") or "").strip():
                        task_result["run_id"] = fallback_run_id
                    ok, failed_test = _consume_chunk_result(chunk, task_result)
                    if not ok:
                        _mark_unresolved_tests(failed_test)
                        for pending in inflight:
                            pending.cancel()
                        pool.shutdown(wait=False, cancel_futures=True)
                        pool_shutdown = True
                        return solve_results
                except Exception as exc:
                    detail = str(exc or "").strip() or "judge backend task failed"
                    chunk = [str(item or "").strip() for item in list(plan.get("tests") or []) if str(item or "").strip()]
                    for name in chunk:
                        solve_results[name] = self._solve_result_error(detail, verdict="FL")
                    failed_anchor = chunk[0] if chunk else ""
                    _mark_unresolved_tests(failed_anchor)
                    for pending in inflight:
                        pending.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool_shutdown = True
                    return solve_results

            while (next_submit_idx < len(plans)) and (len(inflight) < effective_parallelism):
                _submit_plan(next_submit_idx)
                next_submit_idx += 1
    finally:
        if not pool_shutdown:
            pool.shutdown(wait=True, cancel_futures=False)

    for test_name in selected_tests:
        if test_name not in solve_results:
            solve_results[test_name] = self._solve_result_error("judge backend result missing", verdict="FL")
    return solve_results
