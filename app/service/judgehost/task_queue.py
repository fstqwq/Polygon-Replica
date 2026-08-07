from __future__ import annotations

import time
from datetime import datetime, timezone

from app.db import now_iso
from app.service.judgehost.runtime import parse_iso_utc
from app.service.verification.task_scheduler import notify_verification_task_terminal

from app.service.judgehost.case_result import decode_case_test_row
from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.batch_scheduler_models import HostLeaseRelease
from app.service.judgehost.payload_retention import compact_payload_for_retention
from app.service.judgehost.payload_retention import compact_task_row_payload
from app.service.judgehost.state import JudgehostState


class TaskQueue:
    STATUS_ENQUEUING = "enqueuing"
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_REPORTING = "reporting"
    _TASK_KIND_MAIN_CORRECT = "main-correct"

    def __init__(self, state: JudgehostState, core: JudgehostCore) -> None:
        self._s = state
        self._core = core

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

    def record_host_peer_addr(self, hostname: str, peer_addr: str) -> None:
        """Keep the latest source IP as display-only host telemetry.

        The address is never used to authenticate a request or resolve a file;
        the explicit hostname remains the only scheduling/lease label.
        """
        safe_host = self._core.normalize_hostname(hostname)
        safe_peer_addr = str(peer_addr or "").strip()
        if not safe_peer_addr:
            return
        with self._s.state_lock:
            row = self._s.hosts_state.get(safe_host)
            if row is not None:
                row["peer_addr"] = safe_peer_addr

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
        case_result = self._s.batch_scheduler.case_result_for_task(task_id, test_name)
        if case_result is not None:
            verification_id = str(row["verification_id"] or "")
            run_id = str(row["run_id"] or "")
            task_kind = str((row.get("payload") or {}).get("task_kind") or "")
            runresult = case_result.runresult
            verdict = case_result.verdict
            summary = self._task_summary_for_row(
                row,
                run_id=run_id,
                verification_id=verification_id,
            )
            selected_test_row = decode_case_test_row(case_result)
            recovered_error = case_result.feedback_text
            summary_error = str(summary.get("error") or "")
            if (not summary_error) and recovered_error and runresult in {
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
        active_by_host = self._s.batch_scheduler.active_lease_counts()
        telemetry_by_host = self._s.batch_scheduler.host_telemetry_snapshot()
        with self._s.state_lock:
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
            telemetry = telemetry_by_host.get(hostname)
            rows_out.append(
                {
                    "hostname": hostname,
                    "peer_addr": str(row.get("peer_addr") or ""),
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
                    "judged_case_count": 0 if telemetry is None else telemetry["judged_case_count"],
                    "last_judging_at": None if telemetry is None else telemetry["last_judging_at"],
                    "last_judging": None if telemetry is None else telemetry["last_judging"],
                    "recent_avg_per_case_sec": (
                        None if telemetry is None else telemetry["recent_avg_per_case_sec"]
                    ),
                }
            )
        return rows_out, online_count


    def set_host_enabled(self, hostname: str, enabled: bool) -> HostLeaseRelease:
        now_text = now_iso()
        with self._s.state_lock:
            current_row = dict(self._s.hosts_state.get(hostname, {}))
            self._s.hosts_state[hostname] = {
                "hostname": hostname,
                "peer_addr": str(current_row.get("peer_addr") or ""),
                "enabled": bool(enabled),
                "first_seen_at": str(current_row.get("first_seen_at") or now_text),
                "last_seen_at": str(current_row.get("last_seen_at") or now_text),
                "last_action": "set-enabled" if enabled else "set-disabled",
                "last_task_id": str(current_row.get("last_task_id") or ""),
                "last_run_id": str(current_row.get("last_run_id") or ""),
                "update_count": int(current_row.get("update_count") or 0) + 1,
            }
        release = HostLeaseRelease(0, 0, (), (), ())
        if not enabled:
            release = self._s.batch_scheduler.release_host_leases(
                hostname,
                now_text=now_text,
            )
        return release

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

    def cancel_unbatched_verification_tasks(self, verification_id: str, *, reason: str) -> int:
        affected = 0
        now_text = now_iso()
        for row in self._s.task_registry.snapshots():
            if str(row["verification_id"]) != verification_id:
                continue
            task_id = str(row["id"])
            if self._s.batch_scheduler.batch_for_task(task_id) is not None:
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
            if self._s.batch_scheduler.batch_for_task(task_id) is not None:
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


    def cancel_all_domjudge_batches(self) -> list[int]:
        return self._s.batch_scheduler.cancel_all_inflight(now_text=now_iso())

    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        return self._s.batch_scheduler.forget_runs(run_ids)
