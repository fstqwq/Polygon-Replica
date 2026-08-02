from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from app.db import now_iso
from app.service.judgehost.runtime import (
    domjudge_feedback_text_and_files,
    domjudge_feedback_text_from_text,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_verdict_from_runresult,
    parse_iso_utc,
)
from app.service.verification.task_scheduler import notify_verification_task_terminal
from app.service.verification.test_rows import build_verification_test_pass_row, build_verification_test_row

from .core import JudgehostCore
from .payload_retention import compact_payload_for_retention
from .payload_retention import compact_task_row_payload
from .state import JudgehostState
from .toolkit import DomjudgeToolkit


class TaskQueue:
    STATUS_ENQUEUING = "enqueuing"
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_REPORTING = "reporting"
    _TASK_KIND_MAIN_CORRECT = "main-correct"

    def __init__(self, state: JudgehostState, core: JudgehostCore, toolkit: DomjudgeToolkit) -> None:
        self._s = state
        self._core = core
        self._toolkit = toolkit

    @staticmethod
    def compact_payload_for_retention(payload: object) -> dict[str, object]:
        return compact_payload_for_retention(payload)

    def compact_task_payload(self, task_id: str) -> None:
        if not task_id:
            return
        row = self._s.task_registry.get(task_id)
        if row is None:
            return
        compact_task_row_payload(row)
        self._s.task_registry.update(task_id, {"payload": row["payload"]})

    def _run_ids_with_leased_cases(self, run_ids: list[str]) -> set[str]:
        progress = self._s.job_scheduler.case_progress_for_runs(run_ids)
        return {
            run_id
            for run_id, row in progress.items()
            if int(row.get("leased") or 0) > 0
        }

    def domjudge_runs_with_leased_cases(self, run_ids: list[str]) -> set[str]:
        return self._run_ids_with_leased_cases([self._core.normalize_run_id(run_id) for run_id in run_ids if run_id])

    def _record_host_event_conn(
        self,
        *,
        hostname: str,
        action: str,
        task_id: str = "",
        run_id: str = "",
    ) -> None:
        if not action:
            raise RuntimeError("judgehost host event action is required")
        now_text = now_iso()
        with self._s.state_lock:
            row = self._s.hosts_state.get(hostname)
            if row is None:
                row = {
                    "hostname": hostname,
                    "enabled": True,
                    "first_seen_at": now_text,
                    "last_seen_at": now_text,
                    "last_action": "",
                    "last_task_id": "",
                    "last_run_id": "",
                    "update_count": 0,
                }
                self._s.hosts_state[hostname] = row
            row["last_seen_at"] = now_text
            row["last_action"] = action
            row["last_task_id"] = task_id
            row["last_run_id"] = run_id
            row["update_count"] = row["update_count"] + 1

    def _host_enabled_conn(self, conn=None, hostname: str = "") -> bool:
        if not hostname:
            return True
        with self._s.state_lock:
            row = self._s.hosts_state.get(hostname)
            if row is None:
                return True
            return bool(row.get("enabled", True))

    def load_run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        if not run_id:
            return {}
        task_run_id = ""
        task_verification_id = ""
        cached_summary: dict[str, object] | None = None
        task_row = self._s.task_registry.get_for_run(run_id)
        if task_row is not None:
            task_verification_id = str(task_row.get("verification_id") or "")
            task_run_id = str(task_row.get("run_id") or "")
            cached_summary = dict(task_row.get("summary") or {})
        if task_run_id and (
            task_run_id != run_id
            or task_verification_id != verification_id
        ):
            summary = self.load_run_summary(task_run_id, task_verification_id)
            if summary:
                return summary
        if cached_summary is not None:
            return cached_summary
        return {}

    def _task_summary_for_row(
        self,
        row: dict[str, object],
        *,
        run_id: str,
        verification_id: str,
    ) -> dict[str, object]:
        summary = self.load_run_summary(run_id, verification_id)
        if summary:
            return summary
        row_summary = dict(row.get("summary") or {})
        if row_summary:
            return row_summary
        row_result = dict(row.get("result") or {})
        result_summary = dict(row_result.get("summary") or {})
        if result_summary:
            return result_summary
        return {}

    @staticmethod
    def _summary_error_text(summary: dict[str, object]) -> str:
        diagnostics = summary.get("compile_diagnostics") or []
        for item in diagnostics:
            message = item.get("message") or ""
            if message:
                return message
        return summary.get("error") or ""

    def _case_feedback_text_and_files(self, case_row: dict[str, object]) -> tuple[str, list[str]]:
        work_root_token = str(case_row.get("work_root") or "")
        work_root = Path(work_root_token).resolve() if work_root_token else None
        feedback_text, feedback_files = domjudge_feedback_text_and_files(
            read_blob=(
                (lambda token: self._toolkit.read_artifact_blob(work_root, token))
                if work_root is not None
                else (lambda _token: None)
            ),
            runresult=str(case_row.get("runresult") or ""),
            output_error_rel=str(case_row.get("output_error_rel") or ""),
            output_diff_rel=str(case_row.get("output_diff_rel") or ""),
            team_message_rel=str(case_row.get("team_message_rel") or ""),
        )
        if feedback_text:
            return feedback_text, feedback_files
        debug_text = domjudge_feedback_text_from_text(str(case_row.get("debug_text") or ""))
        if debug_text:
            return debug_text, feedback_files
        return "", feedback_files

    def _case_answer_correct(self, case_row: dict[str, object]) -> bool:
        work_root_token = str(case_row.get("work_root") or "")
        compare_metadata_rel = str(case_row.get("compare_metadata_rel") or "")
        if not work_root_token or not compare_metadata_rel:
            return False
        work_root = Path(work_root_token).resolve()
        compare_meta_blob = self._toolkit.read_artifact_blob(work_root, compare_metadata_rel)
        if not compare_meta_blob:
            return False
        compare_meta = domjudge_parse_meta_text(compare_meta_blob.decode("utf-8", errors="replace"))
        return domjudge_parse_int(compare_meta.get("exitcode"), -1) == 42

    def report_result(
        self,
        *,
        task_id: str,
        hostname: str,
        payload: dict[str, object],
        notify_terminal: bool = True,
    ) -> dict[str, object]:
        return self._report_result(
            task_id=task_id,
            hostname=hostname,
            payload=payload,
            notify_terminal=notify_terminal,
            record_host_event=True,
        )

    def finalize_domjudge_task(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
        notify_terminal: bool = True,
    ) -> dict[str, object]:
        return self._report_result(
            task_id=task_id,
            hostname="internal-finalizer",
            payload=payload,
            notify_terminal=notify_terminal,
            record_host_event=False,
        )

    def _report_result(
        self,
        *,
        task_id: str,
        hostname: str,
        payload: dict[str, object],
        notify_terminal: bool,
        record_host_event: bool,
    ) -> dict[str, object]:
        if not task_id:
            raise RuntimeError("task_id is required")
        hostname = self._core.normalize_hostname(hostname)
        run_status = payload["run_status"]
        run_status_token = run_status.lower()
        if run_status_token in {"ok", "accepted", "pass", "passed", "success", "completed"}:
            run_status = "ok"
            task_status = self.STATUS_COMPLETED
        else:
            run_status = "failed"
            task_status = self.STATUS_FAILED
        error_text = (payload.get("error") or "").strip()

        row = self._s.task_registry.claim_reporting(
            task_id,
            now_text=now_iso(),
        )
        if row is None:
            raise RuntimeError("judgehost task not found")
        current_status = str(row["status"])
        if current_status in {self.STATUS_COMPLETED, self.STATUS_FAILED}:
            run_id = str(row.get("run_id") or "")
            verification_id = str(row.get("verification_id") or "")
            summary = row.get("summary")
            return {
                "task_id": task_id,
                "verification_id": verification_id,
                "run_id": run_id,
                "artifact_path": "",
                "status": str(
                    row.get("run_status")
                    or ("ok" if current_status == self.STATUS_COMPLETED else "failed")
                ),
                "summary": dict(summary) if isinstance(summary, dict) else {},
            }
        run_id = str(row["run_id"])
        verification_id = str(row["verification_id"])
        prev_summary = dict(row.get("summary") or {})

        try:
            existing_summary = self.load_run_summary(run_id, verification_id) or prev_summary
            summary = payload.get("summary")
            summary_obj = dict(existing_summary) if summary is None else {**existing_summary, **summary}
            if run_status != "ok":
                if error_text:
                    summary_obj["error"] = error_text
                elif "error" not in summary_obj:
                    summary_obj["error"] = "judgehost reported failure"
            summary_obj["status"] = run_status

            judgehost_block = dict(summary_obj.get("judgehost") or {})
            judgehost_block["task_id"] = task_id
            judgehost_block["hostname"] = hostname
            judgehost_block["status"] = task_status
            summary_obj["judgehost"] = judgehost_block

            if (not error_text) and run_status != "ok":
                error_text = self._summary_error_text(summary_obj) or "judgehost task failed"

            finished_at = now_iso()
        except Exception:
            self._s.task_registry.restore_reporting(task_id, row, now_text=now_iso())
            raise

        completed = self._s.task_registry.transition(
            task_id,
            expected={self.STATUS_REPORTING},
            status=task_status,
            updates={
                "payload": compact_payload_for_retention(row.get("payload")),
                "result": {
                    "run_status": run_status,
                    "error": error_text,
                    "summary": dict(summary_obj),
                },
                "summary": dict(summary_obj),
                "run_status": run_status,
                "error_text": error_text,
                "updated_at": finished_at,
                "completed_at": finished_at,
            },
        )
        if completed is None:
            raise RuntimeError("judgehost task reporting claim was lost")
        if record_host_event:
            self._record_host_event_conn(
                hostname=hostname,
                action="report",
                task_id=task_id,
                run_id=run_id,
            )
        if verification_id and notify_terminal:
            notify_verification_task_terminal(verification_id, task_id)
        return {
            "task_id": task_id,
            "verification_id": verification_id,
            "run_id": run_id,
            "artifact_path": "",
            "status": run_status,
            "summary": summary_obj,
        }

    def wait_for_task_result(self, task_id: str, timeout_sec: float | None = None) -> dict[str, object]:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        timeout = self._s.wait_timeout_sec if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        generation = self._s.task_registry.change_generation()
        while True:
            result = self.poll_task_result(task_id)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            generation = self._s.task_registry.wait_for_change(generation, remaining)

    def poll_task_result(self, task_id: str) -> dict[str, object] | None:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        row = self._core.task_by_id(task_id)
        if row is None:
            raise RuntimeError("judgehost task disappeared")
        status = str(row["status"] or "")
        if status not in {self.STATUS_COMPLETED, self.STATUS_FAILED}:
            return None
        verification_id = str(row["verification_id"] or "")
        run_id = str(row["run_id"] or "")
        return {
            "task_id": task_id,
            "verification_id": verification_id,
            "run_id": run_id,
            "artifact_path": "",
            "status": row["run_status"],
            "task_status": status,
            "error": row["error_text"],
            "summary": dict(row.get("summary") or {}),
        }

    def wait_for_task_case_result(self, task_id: str, test_name: str, timeout_sec: float | None = None) -> dict[str, object]:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        if not test_name:
            raise RuntimeError("judgehost test name is required")
        timeout = self._s.wait_timeout_sec if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        generation = self._s.task_registry.change_generation()
        while True:
            result = self.poll_task_case_result(task_id, test_name)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            generation = self._s.task_registry.wait_for_change(generation, remaining)

    def poll_task_case_result(self, task_id: str, test_name: str) -> dict[str, object] | None:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        if not test_name:
            raise RuntimeError("judgehost test name is required")
        row = self._core.task_by_id(task_id)
        if row is None:
            raise RuntimeError("judgehost task disappeared")
        case_row = self._s.job_scheduler.case_for_task(task_id, test_name)
        if case_row is not None and str(case_row["status"] or "") == "reported":
            verification_id = str(row["verification_id"] or "")
            run_id = str(row["run_id"] or "")
            task_kind = str((row.get("payload") or {}).get("task_kind") or "")
            runresult = str(case_row["runresult"] or "")
            verdict = domjudge_verdict_from_runresult(runresult)
            summary = self._task_summary_for_row(
                row,
                run_id=run_id,
                verification_id=verification_id,
            )
            tests = summary.get("tests")
            selected_test_row = None
            if isinstance(tests, list):
                for item in tests:
                    if isinstance(item, dict) and str(item.get("test") or "") == test_name:
                        selected_test_row = dict(item)
                        break
            if selected_test_row is None:
                answer_correct = self._case_answer_correct(case_row)
                cpu_ms = max(0, int(round(float(case_row["cpu_sec"] or case_row["runtime_sec"] or 0.0) * 1000.0)))
                wall_ms = max(0, int(round(float(case_row["wall_sec"] or case_row["cpu_sec"] or case_row["runtime_sec"] or 0.0) * 1000.0)))
                memory_kb = max(0, int(case_row["memory_kb"] or 0))
                feedback_text, feedback_files = self._case_feedback_text_and_files(case_row)
                selected_test_row = build_verification_test_row(
                    test_name=test_name,
                    verdict=domjudge_verdict_from_runresult(str(case_row["runresult"] or "")),
                    time_ms=cpu_ms,
                    time_user_ms=cpu_ms,
                    time_wall_ms=wall_ms,
                    memory_kb=memory_kb,
                    message=feedback_text,
                    output_ref=str(case_row["output_run_rel"] or ""),
                    feedback_files=feedback_files,
                    passes=[
                        build_verification_test_pass_row(
                            verdict=domjudge_verdict_from_runresult(str(case_row["runresult"] or "")),
                            time_ms=cpu_ms,
                            time_user_ms=cpu_ms,
                            time_wall_ms=wall_ms,
                            memory_kb=memory_kb,
                            feedback=feedback_text,
                            output_ref=str(case_row["output_run_rel"] or ""),
                            runresult=str(case_row["runresult"] or ""),
                            answer_correct=answer_correct,
                        )
                    ],
                    runresult=str(case_row["runresult"] or ""),
                    answer_correct=answer_correct,
                )
                recovered_error = feedback_text
            else:
                selected_test_row["answer_correct"] = bool(
                    selected_test_row.get("answer_correct") or self._case_answer_correct(case_row)
                )
                recovered_error = str(selected_test_row.get("message") or "")
                if not recovered_error:
                    feedback_text, feedback_files = self._case_feedback_text_and_files(case_row)
                    if feedback_text:
                        selected_test_row["message"] = feedback_text
                        recovered_error = feedback_text
                    if feedback_files and not list(selected_test_row.get("feedback_files") or []):
                        selected_test_row["feedback_files"] = feedback_files
                    if (not str(selected_test_row.get("output_ref") or "")) and str(case_row.get("output_run_rel") or ""):
                        selected_test_row["output_ref"] = str(case_row.get("output_run_rel") or "")
            summary_error = str(summary.get("error") or "")
            if (not summary_error) and recovered_error and str(case_row["runresult"] or "") in {
                "checker-fail",
                "compare-error",
                "internal-error",
            }:
                summary_error = recovered_error
            if (not summary_error) and recovered_error and task_kind == self._TASK_KIND_MAIN_CORRECT and verdict != "OK":
                summary_error = recovered_error
            case_summary = {
                "source": summary.get("source") or "",
                "compile_diagnostics": list(summary.get("compile_diagnostics") or []),
                "error": summary_error,
                "tests": [selected_test_row],
            }
            if task_kind == self._TASK_KIND_MAIN_CORRECT:
                run_status = "ok" if verdict == "OK" else "failed"
            elif runresult in {"compiler-error", "checker-fail", "compare-error", "internal-error"}:
                run_status = "failed"
            else:
                run_status = "ok"
            return {
                "task_id": task_id,
                "verification_id": str(row["verification_id"] or ""),
                "run_id": str(row["run_id"] or ""),
                "artifact_path": "",
                "status": run_status,
                "task_status": row["status"],
                "error": str(case_summary.get("error") or ""),
                "summary": case_summary,
            }
        task_status = str(row["status"] or "")
        if task_status in {self.STATUS_FAILED, self.STATUS_COMPLETED}:
            verification_id = str(row["verification_id"] or "")
            run_id = str(row["run_id"] or "")
            task_summary = self._task_summary_for_row(
                row,
                run_id=run_id,
                verification_id=verification_id,
            )
            source_label = str(task_summary.get("source") or "")
            compile_diagnostics = list(task_summary.get("compile_diagnostics") or [])
            detail = str(task_summary.get("error") or row.get("error_text") or "")
            if not detail:
                detail = f"judgehost case result missing for {test_name}"
            return {
                "task_id": task_id,
                "verification_id": verification_id,
                "run_id": run_id,
                "artifact_path": "",
                "status": "failed",
                "task_status": task_status,
                "missing_case_result": True,
                "error": detail,
                "summary": {
                    "source": source_label,
                    "compile_diagnostics": compile_diagnostics,
                    "error": detail,
                    "tests": [],
                },
            }
        return None

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        result = self.wait_for_task_result(task_id, timeout_sec=timeout_sec)
        task_status = result["task_status"]
        if task_status == self.STATUS_FAILED:
            detail = result["error"] or "judgehost task failed without error text"
            raise RuntimeError(detail)
        return result["run_id"]

    def _host_status_rows(self) -> tuple[list[dict[str, object]], int]:
        now_dt = datetime.now(timezone.utc)
        now_ts = time.time()
        active_by_host = self._s.job_scheduler.active_lease_counts()
        cases_5m: dict[str, int] = {}
        cases_15m: dict[str, int] = {}
        cases_1h: dict[str, int] = {}
        cases_5h: dict[str, int] = {}
        with self._s.state_lock:
            for host, events in self._s.host_judged_case_events.items():
                cutoff = now_ts - (5 * 3600.0)
                while events and events[0] < cutoff:
                    events.popleft()
                c5 = 0
                c15 = 0
                c1h = 0
                c5h = 0
                for ts in events:
                    age = now_ts - float(ts)
                    if age <= 5 * 3600:
                        c5h += 1
                        if age <= 3600:
                            c1h += 1
                            if age <= 900:
                                c15 += 1
                                if age <= 300:
                                    c5 += 1
                cases_5m[host] = c5
                cases_15m[host] = c15
                cases_1h[host] = c1h
                cases_5h[host] = c5h
            last_judging_by_host = {k: dict(v) for k, v in self._s.host_last_judging.items()}
            host_rows = sorted(
                (dict(row) for row in self._s.hosts_state.values()),
                key=lambda item: (
                    str(item.get("last_seen_at") or ""),
                    str(item.get("hostname") or ""),
                ),
                reverse=True,
            )
        rows_out: list[dict[str, object]] = []
        online_count = 0
        for row in host_rows:
            hostname = str(row.get("hostname") or "")
            if not hostname:
                continue
            enabled_flag = bool(row.get("enabled"))
            last_seen = str(row.get("last_seen_at") or "")
            last_seen_dt = parse_iso_utc(last_seen)
            age_sec: int | None = None
            is_online = False
            if last_seen_dt is not None:
                delta = max(0.0, (now_dt - last_seen_dt).total_seconds())
                age_sec = int(delta)
                is_online = delta <= float(self._s.online_window_sec)
            if is_online and enabled_flag:
                online_count += 1
            count_5m = int(cases_5m.get(hostname, 0))
            count_15m = int(cases_15m.get(hostname, 0))
            count_1h = int(cases_1h.get(hostname, 0))
            count_5h = int(cases_5h.get(hostname, 0))
            last_judging = dict(last_judging_by_host.get(hostname, {}))
            last_judging_label = last_judging.get("label")
            last_judging_at = last_judging.get("updated_at")
            rows_out.append(
                {
                    "hostname": hostname,
                    "enabled": enabled_flag,
                    "online": is_online,
                    "age_sec": age_sec,
                    "last_seen_at": last_seen,
                    "first_seen_at": str(row.get("first_seen_at") or ""),
                    "last_action": str(row.get("last_action") or ""),
                    "last_task_id": str(row.get("last_task_id") or ""),
                    "last_run_id": str(row.get("last_run_id") or ""),
                    "active_leases": int(active_by_host.get(hostname, 0)),
                    "update_count": int(row.get("update_count") or 0),
                    "load_5m": float(count_5m / 300.0),
                    "load_15m": float(count_15m / 900.0),
                    "load_1h": float(count_1h / 3600.0),
                    "load_5h": float(count_5h / 18000.0),
                    "judged_cases_5m": count_5m,
                    "judged_cases_15m": count_15m,
                    "judged_cases_1h": count_1h,
                    "judged_cases_5h": count_5h,
                    "last_judging": last_judging_label,
                    "last_judging_at": last_judging_at,
                }
            )
        return rows_out, online_count


    def set_host_enabled(self, hostname: str, enabled: bool) -> dict[str, int]:
        now_text = now_iso()
        with self._s.state_lock:
            current_row = dict(self._s.hosts_state.get(hostname, {}))
            self._s.hosts_state[hostname] = {
                "hostname": hostname,
                "enabled": bool(enabled),
                "first_seen_at": str(current_row.get("first_seen_at") or now_text),
                "last_seen_at": str(current_row.get("last_seen_at") or now_text),
                "last_action": "set-enabled" if enabled else "set-disabled",
                "last_task_id": str(current_row.get("last_task_id") or ""),
                "last_run_id": str(current_row.get("last_run_id") or ""),
                "update_count": int(current_row.get("update_count") or 0) + 1,
            }
        released_jobs = 0
        released_cases = 0
        if not enabled:
            released_jobs, released_cases = self._s.job_scheduler.release_host_leases(
                hostname,
                now_text=now_text,
            )
        return {
            "released_tasks": 0,
            "released_jobs": released_jobs,
            "released_cases": released_cases,
        }

    def status(self) -> dict[str, object]:
        counts = self._core.task_status_counts()
        host_rows, online_count = self._host_status_rows()
        return {
            "enabled": bool(self._s.enabled),
            "auth_configured": bool(self._s.api_token),
            "hosts_total": len(host_rows),
            "hosts_online": int(online_count),
            "hosts": host_rows,
            "queue": {
                "queued": int(counts.get(self.STATUS_QUEUED, 0)),
                "leased": int(counts.get(self.STATUS_LEASED, 0)),
                "completed": int(counts.get(self.STATUS_COMPLETED, 0)),
                "failed": int(counts.get(self.STATUS_FAILED, 0)),
            },
        }

    def cancel_tasks_for_runs(self, run_ids: list[str], *, reason: str) -> int:
        reason = reason.strip()
        if not reason:
            raise RuntimeError("judgehost cancellation reason is required")
        run_ids = [run_id for run_id in run_ids if run_id]
        if not run_ids:
            return 0
        now_text = now_iso()
        affected = 0
        for run_id in run_ids:
            row = self._s.task_registry.get_for_run(run_id)
            if row is None:
                continue
            task_id = str(row["id"])
            if self._s.job_scheduler.job_for_task(task_id) is not None:
                continue
            updated = self._s.task_registry.transition(
                task_id,
                expected={self.STATUS_ENQUEUING, self.STATUS_QUEUED, self.STATUS_LEASED},
                status=self.STATUS_FAILED,
                updates={
                    "payload": compact_payload_for_retention(row.get("payload")),
                    "result": {"cancelled": True, "reason": reason, "error": reason},
                    "error_text": reason,
                    "updated_at": now_text,
                    "completed_at": now_text,
                },
            )
            affected += int(updated is not None)
        return affected


    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[dict[str, str]]:
        reason = reason.strip()
        if not reason:
            raise RuntimeError("judgehost startup cancel reason is required")
        now_text = now_iso()
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in self._s.task_registry.snapshots():
            status = row["status"]
            if status not in {
                self.STATUS_ENQUEUING,
                self.STATUS_QUEUED,
                self.STATUS_LEASED,
            }:
                continue
            run_id = str(row["run_id"])
            verification_id = str(row["verification_id"])
            task_id = str(row["id"])
            entry_key = (verification_id, run_id)
            if verification_id and run_id and entry_key not in seen:
                seen.add(entry_key)
                entries.append({"run_id": run_id, "verification_id": verification_id})
            if self._s.job_scheduler.job_for_task(task_id) is not None:
                continue
            self._s.task_registry.transition(
                task_id,
                expected={self.STATUS_ENQUEUING, self.STATUS_QUEUED, self.STATUS_LEASED},
                status=self.STATUS_FAILED,
                updates={
                    "payload": compact_payload_for_retention(row.get("payload")),
                    "result": {"cancelled": True, "reason": reason, "error": reason},
                    "error_text": reason,
                    "updated_at": now_text,
                    "completed_at": now_text,
                },
            )
        return entries

    def forget_problem_tasks(self, problem_slug: str) -> int:
        if not problem_slug:
            return 0
        return self._s.task_registry.remove_problem(problem_slug)


    def cancel_domjudge_jobs_for_runs(self, run_ids: list[str]) -> list[int]:
        return self._s.job_scheduler.cancel_jobs_for_runs(
            run_ids=run_ids,
            now_text=now_iso(),
        )


    def cancel_all_domjudge_inflight(self) -> list[int]:
        return self._s.job_scheduler.cancel_all_inflight(now_text=now_iso())

    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        return self._s.job_scheduler.forget_runs(run_ids)
