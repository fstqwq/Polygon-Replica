from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app.db import now_iso
from app.service.judgehost.runtime import now_iso_after, parse_iso_utc
from app.service.verification.store import load_verification_run, save_verification_run_summary


class JudgehostQueueMixin:

    def _claim_lease_requeue_slot(self, *, interval_sec: float = 0.75) -> bool:
        now_mono = time.monotonic()
        with self._lease_requeue_lock:
            if now_mono < float(self._lease_requeue_next_ts):
                return False
            self._lease_requeue_next_ts = now_mono + max(0.05, float(interval_sec))
            return True

    def _requeue_expired_leases(self, *, force: bool = False) -> None:
        if (not force) and (not self._claim_lease_requeue_slot()):
            return
        now_dt = datetime.now(timezone.utc)
        now_text = now_dt.isoformat()
        with self._state_lock:
            for task in self._tasks_by_id.values():                if task["status"] != self.STATUS_LEASED:
                    continue
                lease_exp = parse_iso_utc(task.get("lease_expires_at"))
                if lease_exp is None or lease_exp >= now_dt:
                    continue
                task["status"] = self.STATUS_QUEUED
                task["lease_owner"] = ""
                task["lease_expires_at"] = ""
                task["updated_at"] = now_text

    def _record_host_event_conn(
        self,
        *,
        hostname: str,
        action: str,
        task_id: str = "",
        run_id: str = "",
        lease_expires_at: str = "",
    ) -> None:
        if not action:
            raise RuntimeError("judgehost host event action is required")
        now_text = now_iso()
        with self._state_lock:
            row = self._hosts_state.get(hostname)
            if row is None:
                row = {
                    "hostname": hostname,
                    "enabled": True,
                    "first_seen_at": now_text,
                    "last_seen_at": now_text,
                    "last_action": "",
                    "last_task_id": "",
                    "last_run_id": "",
                    "lease_expires_at": "",
                    "update_count": 0,
                }
                self._hosts_state[hostname] = row
            row["last_seen_at"] = now_text
            row["last_action"] = action
            row["last_task_id"] = task_id
            row["last_run_id"] = run_id
            row["lease_expires_at"] = lease_expires_at
            row["update_count"] = row["update_count"] + 1

    def _host_enabled_conn(self, conn=None, hostname: str = "") -> bool:
        if not hostname:
            return True
        with self._state_lock:
            row = self._hosts_state.get(hostname)
            if row is None:
                return True
            return bool(row.get("enabled", True))

    def fetch_work(self, hostname: str, limit: int | None = None) -> list[dict[str, object]]:
        hostname = self._normalize_hostname(hostname)
        cap = self._fetch_batch_size if limit is None else max(1, min(256, int(limit)))
        tasks: list[dict[str, object]] = []
        self._requeue_expired_leases()
        event_action = "fetch"
        event_task_id = ""
        event_run_id = ""
        event_lease_expires_at = ""
        with self._state_lock:
            host_row = self._hosts_state.get(hostname)
            if host_row is not None and not bool(host_row.get("enabled", True)):
                event_action = "disabled"
            else:
                queued = [
                    task
                    for task in self._tasks_by_id.values()
                    if task["status"] == self.STATUS_QUEUED
                ]
                queued.sort(key=lambda item: item["created_at"])
                lease_until = now_iso_after(self._lease_sec)
                now_text = now_iso()
                for task in queued[:cap]:
                    task_id = task["id"]
                    if task["status"] != self.STATUS_QUEUED:
                        continue
                    task["status"] = self.STATUS_LEASED
                    task["lease_owner"] = hostname
                    task["lease_expires_at"] = lease_until
                    task["updated_at"] = now_text
                    task["attempt_count"] = task["attempt_count"] + 1
                    payload = task.get("payload") or {}
                    tasks.append(
                        {
                            "task_id": task_id,
                            "run_id": task["run_id"],
                            "problem": task["problem_slug"],
                            "username": task["username"],
                            "artifact_verification_id": task["artifact_verification_id"],
                            "mode": task["mode"],
                            "lease_expires_at": lease_until,
                            "payload": dict(payload),
                        }
                    )
                if tasks:
                    tail = tasks[-1]
                    event_action = "lease"
                    event_task_id = tail["task_id"]
                    event_run_id = tail["run_id"]
                    event_lease_expires_at = lease_until
        self._record_host_event_conn(
            hostname=hostname,
            action=event_action,
            task_id=event_task_id,
            run_id=event_run_id,
            lease_expires_at=event_lease_expires_at,
        )
        return tasks

    def renew_lease(self, task_id: str, hostname: str) -> bool:
        hostname = self._normalize_hostname(hostname)
        if not task_id:
            return False
        with self._state_lock:
            task = self._tasks_by_id.get(task_id)
            if task is None:
                self._record_host_event_conn(hostname=hostname, action="heartbeat", task_id=task_id)
                return False
            if task["status"] != self.STATUS_LEASED:
                self._record_host_event_conn(hostname=hostname, action="heartbeat", task_id=task_id)
                return False
            if task["lease_owner"] != hostname:
                self._record_host_event_conn(hostname=hostname, action="heartbeat", task_id=task_id)
                return False
            now_text = now_iso()
            lease_until = now_iso_after(self._lease_sec)
            task["lease_expires_at"] = lease_until
            task["updated_at"] = now_text
            self._record_host_event_conn(
                hostname=hostname,
                action="heartbeat",
                task_id=task_id,
                lease_expires_at=lease_until,
            )
            return True

    def _load_run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        if not run_id:
            return {}
        if verification_id:
            run_row = load_verification_run(
                self.db,
                verification_id=verification_id,
                run_id=run_id,
            )
            summary = run_row.get("summary") or {}
            if summary:
                return dict(summary)
        run_row = load_verification_run(
            self.db,
            verification_id=f"ver-{run_id}",
            run_id=run_id,
        )
        summary = run_row.get("summary") or {}
        if summary:
            return dict(summary)
        task_run_id = ""
        task_verification_id = ""
        cached_summary: dict[str, object] | None = None
        with self._state_lock:
            task_id = self._task_id_by_run.get(run_id)
            if task_id is not None:
                task_row = self._tasks_by_id.get(task_id)
                if task_row is not None:
                    task_verification_id = task_row.get("verification_id")
                    task_run_id = task_row.get("run_id")
                    cached_summary = dict(task_row.get("summary") or {})
        if task_run_id and (
            task_run_id != run_id
            or task_verification_id != verification_id
        ):
            summary = self._load_run_summary(task_run_id, task_verification_id)
            if summary:
                return summary
        if cached_summary is not None:
            return cached_summary
        return {}

    @staticmethod
    def _summary_error_text(summary: dict[str, object]) -> str:
        diagnostics = summary.get("compile_diagnostics") or []
        for item in diagnostics:
            message = item.get("message") or ""
            if message:
                return message
        return summary.get("error") or ""

    def report_result(self, *, task_id: str, hostname: str, payload: dict[str, object]) -> dict[str, object]:
        if not task_id:
            raise RuntimeError("task_id is required")
        hostname = self._normalize_hostname(hostname)
        run_status = payload["run_status"]
        run_status_token = run_status.lower()
        if run_status_token in {"ok", "accepted", "pass", "passed", "success", "completed"}:
            run_status = "ok"
            task_status = self.STATUS_COMPLETED
        else:
            run_status = "failed"
            task_status = self.STATUS_FAILED
        error_text = (payload.get("error") or "").strip()

        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is None:
                raise RuntimeError("judgehost task not found")
            current_status = row["status"]
            lease_owner = row["lease_owner"]
            if current_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                raise RuntimeError(f"judgehost task is not reportable (status={current_status})")
            if lease_owner and lease_owner != hostname:
                raise RuntimeError("judgehost task lease owner mismatch")
            run_id = row["run_id"]
            verification_id = row["verification_id"]
            prev_status = current_status
            prev_lease_owner = lease_owner
            prev_lease_expires_at = row["lease_expires_at"]
            prev_error_text = row["error_text"]
            prev_result = dict(row.get("result") or {})
            prev_summary = dict(row.get("summary") or {})
            persist_verification_run = bool(row.get("persist_verification_run", True))
            row["status"] = self.STATUS_REPORTING
            row["updated_at"] = now_iso()

        try:
            existing_summary = self._load_run_summary(run_id, verification_id) or prev_summary
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
            if persist_verification_run:
                self._ensure_verification_result(
                    row=row,
                    verification_id=verification_id,
                    run_id=run_id,
                    run_status=run_status,
                    summary_obj=summary_obj,
                    error_text=error_text,
                )
        except Exception:
            with self._state_lock:
                row = self._tasks_by_id.get(task_id)
                if row is not None and row["status"] == self.STATUS_REPORTING:
                    row["status"] = prev_status
                    row["lease_owner"] = prev_lease_owner
                    row["lease_expires_at"] = prev_lease_expires_at
                    row["error_text"] = prev_error_text
                    row["result"] = prev_result
                    row["summary"] = prev_summary
                    row["updated_at"] = now_iso()
            raise

        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is not None:
                row["status"] = task_status
                row["result"] = dict(payload)
                row["summary"] = dict(summary_obj)
                row["run_status"] = run_status
                row["error_text"] = error_text
                row["lease_owner"] = hostname
                row["lease_expires_at"] = ""
                row["updated_at"] = finished_at
                row["completed_at"] = finished_at
        self._record_host_event_conn(
            hostname=hostname,
            action="report",
            task_id=task_id,
            run_id=run_id,
        )
        run_artifact_path: str | None = None
        if persist_verification_run:
            try:
                run_root = self._fs_manager.prepare_verification_run_root(
                    verification_id,
                    run_id,
                ).resolve()
                (run_root / "summary.json").write_text(
                    json.dumps(summary_obj, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                run_artifact_path = str(run_root)
            except Exception:
                run_artifact_path = None
        return {
            "task_id": task_id,
            "verification_id": verification_id,
            "run_id": run_id,
            "artifact_path": run_artifact_path,
            "status": run_status,
            "summary": summary_obj,
        }

    def _ensure_verification_result(
        self,
        *,
        row: dict[str, object],
        verification_id: str,
        run_id: str,
        run_status: str,
        summary_obj: dict[str, object],
        error_text: str,
    ) -> None:
        if not verification_id or not run_id:
            raise RuntimeError("verification result requires verification_id and run_id")
        problem_slug = row["problem_slug"]
        username = row["username"]
        ctx = self._workspace_service.workspace_context(problem_slug, username, include_recent=False)
        run_root = self._fs_manager.prepare_verification_run_root(verification_id, run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        payload = row.get("payload") or {}
        verification_source = payload.get("verification_source") or ""
        if not verification_source:
            raise RuntimeError("judgehost payload missing verification_source")
        mode = row["mode"]
        source_label = summary_obj.get("source") or ""
        if not source_label:
            raise RuntimeError("judgehost summary missing source")
        expected_behavior = payload.get("expected_behavior") or ""
        if not expected_behavior:
            raise RuntimeError("judgehost payload missing expected_behavior")
        task_kind = payload.get("task_kind") or ""
        save_verification_run_summary(
            self.db,
            self._fs_manager,
            verification_id=verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit="",
            source_ref="",
            kind="verification",
            mode=mode,
            verification_source=verification_source,
            source_paths=[source_label],
            run_id=run_id,
            run_status=run_status,
            source_label=source_label,
            expected_behavior=expected_behavior,
            run_summary=summary_obj,
            artifact_path=str(run_root),
            task_kind=task_kind,
            error_text=error_text,
        )

    def wait_for_task_result(self, task_id: str, timeout_sec: float | None = None) -> dict[str, object]:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        timeout = self._wait_timeout_sec if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        while True:
            row = self._task_by_id(task_id)
            if row is None:
                raise RuntimeError("judgehost task disappeared")
            status = row["status"]
            if status in {self.STATUS_COMPLETED, self.STATUS_FAILED}:
                verification_id = row["verification_id"]
                run_id = row["run_id"]
                artifact_path = str(
                    self._fs_manager.prepare_verification_run_root(
                        verification_id,
                        run_id,
                    ).resolve()
                )
                return {
                    "task_id": task_id,
                    "verification_id": verification_id,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "status": row["run_status"],
                    "task_status": status,
                    "error": row["error_text"],
                    "summary": dict(row.get("summary") or {}),
                }
            if time.monotonic() >= deadline:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            time.sleep(self._wait_poll_sec)

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
        active_by_host: dict[str, int] = {}
        cases_5m: dict[str, int] = {}
        cases_15m: dict[str, int] = {}
        cases_1h: dict[str, int] = {}
        cases_5h: dict[str, int] = {}
        with self._state_lock:
            for task in self._tasks_by_id.values():
                if task["status"] != self.STATUS_LEASED:
                    continue
                host = task["lease_owner"]
                if not host:
                    continue
                lease_expires_at = parse_iso_utc(task.get("lease_expires_at"))
                if lease_expires_at is None or lease_expires_at < now_dt:
                    continue
                active_by_host[host] = int(active_by_host.get(host, 0)) + 1
            for host, events in self._host_judged_case_events.items():
                keep: list[float] = []
                c5 = 0
                c15 = 0
                c1h = 0
                c5h = 0
                for ts in events:
                    age = now_ts - float(ts)
                    if age <= 5 * 3600:
                        keep.append(ts)
                        c5h += 1
                        if age <= 3600:
                            c1h += 1
                            if age <= 900:
                                c15 += 1
                                if age <= 300:
                                    c5 += 1
                self._host_judged_case_events[host] = keep
                cases_5m[host] = c5
                cases_15m[host] = c15
                cases_1h[host] = c1h
                cases_5h[host] = c5h
            last_judging_by_host = {k: dict(v) for k, v in self._host_last_judging.items()}
            host_rows = sorted(
                (dict(row) for row in self._hosts_state.values()),
                key=lambda item: (item["last_seen_at"], item["hostname"]),
                reverse=True,
            )
        rows_out: list[dict[str, object]] = []
        online_count = 0
        for row in host_rows:
            hostname = row["hostname"]
            if not hostname:
                continue
            enabled_flag = bool(row["enabled"])
            last_seen = row["last_seen_at"]
            last_seen_dt = parse_iso_utc(last_seen)
            age_sec: int | None = None
            is_online = False
            if last_seen_dt is not None:
                delta = max(0.0, (now_dt - last_seen_dt).total_seconds())
                age_sec = int(delta)
                is_online = delta <= float(self._online_window_sec)
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
                    "first_seen_at": row["first_seen_at"],
                    "last_action": row["last_action"],
                    "last_task_id": row["last_task_id"],
                    "last_run_id": row["last_run_id"],
                    "lease_expires_at": row["lease_expires_at"],
                    "active_leases": int(active_by_host.get(hostname, 0)),
                    "update_count": row["update_count"],
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
        released_tasks = 0
        with self._state_lock:
            self._hosts_state[hostname] = {
                "enabled": bool(enabled),
                "first_seen_at": self._hosts_state.get(hostname, {}).get("first_seen_at", now_text),
                "last_action": "set-enabled" if enabled else "set-disabled",
                "last_task_id": self._hosts_state.get(hostname, {}).get("last_task_id", ""),
                "last_run_id": self._hosts_state.get(hostname, {}).get("last_run_id", ""),
                "lease_expires_at": "",
                "update_count": int(self._hosts_state.get(hostname, {}).get("update_count", 0)) + 1,
            }
            if not enabled:
                for task in self._tasks_by_id.values():
                    if task["lease_owner"] != hostname:
                        continue
                    task_status = task["status"]
                    if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                        continue
                    task["status"] = self.STATUS_QUEUED
                    task["lease_owner"] = ""
                    task["lease_expires_at"] = ""
                    task["updated_at"] = now_text
                    released_tasks += 1
        released_jobs = 0
        released_cases = 0
        if not enabled:
            released_jobs, released_cases = self._judgehost_state_store.release_host_leases(
                hostname,
                now_text=now_text,
            )
        return {
            "released_tasks": released_tasks,
            "released_jobs": released_jobs,
            "released_cases": released_cases,
        }

    def status(self) -> dict[str, object]:
        counts = self._task_status_counts()
        host_rows, online_count = self._host_status_rows()
        return {
            "enabled": bool(self._enabled),
            "auth_configured": bool(self._api_token),
            "auth_username": self.api_username(),
            "fetch_batch_size": self._fetch_batch_size,
            "lease_sec": self._lease_sec,
            "wait_timeout_sec": self._wait_timeout_sec,
            "wait_poll_sec": self._wait_poll_sec,
            "online_window_sec": self._online_window_sec,
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
        with self._state_lock:
            for run_id in run_ids:
                task_id = self._task_id_by_run.get(run_id)
                if task_id is None:
                    continue
                row = self._tasks_by_id.get(task_id)
                if row is None:
                    continue
                if row["status"] not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": reason, "error": reason}
                row["error_text"] = reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
                affected += 1
        return affected

    def active_task_count_for_verification(self, verification_id: str) -> int:
        if not verification_id:
            return 0
        count = 0
        with self._state_lock:
            for row in self._tasks_by_id.values():
                artifact_verification_id = row["artifact_verification_id"]
                if artifact_verification_id != verification_id:
                    continue
                if row["status"] in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    count += 1
        return count

    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[dict[str, str]]:
        reason = reason.strip()
        if not reason:
            raise RuntimeError("judgehost startup cancel reason is required")
        now_text = now_iso()
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        with self._state_lock:
            for row in self._tasks_by_id.values():
                status = row["status"]
                if status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                run_id = row["run_id"]
                verification_id = row["verification_id"]
                entry_key = (verification_id, run_id)
                if verification_id and run_id and entry_key not in seen:
                    seen.add(entry_key)
                    entries.append(
                        {
                            "run_id": run_id,
                            "verification_id": verification_id,
                        }
                    )
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": reason, "error": reason}
                row["error_text"] = reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
        return entries

    def forget_problem_tasks(self, problem_slug: str) -> int:
        if not problem_slug:
            return 0
        removed = 0
        with self._state_lock:
            remove_ids = [
                task_id
                for task_id, row in self._tasks_by_id.items()
                if row["problem_slug"] == problem_slug
            ]
            for task_id in remove_ids:
                row = self._tasks_by_id.pop(task_id, None)
                if row is None:
                    continue
                run_id = row.get("run_id")
                if run_id and self._task_id_by_run.get(run_id) == task_id:
                    self._task_id_by_run.pop(run_id, None)
                removed += 1
        return removed


    def cancel_domjudge_jobs_for_runs(self, run_ids: list[str], *, final_status: str = "failed") -> int:
        return self._judgehost_state_store.cancel_jobs_for_runs(
            run_ids=run_ids,
            final_status=(final_status or "failed"),
            now_text=now_iso(),
        )


    def cancel_all_domjudge_inflight(self) -> int:
        return self._judgehost_state_store.cancel_all_inflight(now_text=now_iso())

    def domjudge_case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        safe_run_ids = [self._normalize_run_id(run_id) for run_id in run_ids if run_id]
        return self._judgehost_state_store.case_progress_for_runs(safe_run_ids)

    def domjudge_case_cells_for_runs(self, run_ids: list[str]) -> list[dict[str, object]]:
        safe_run_ids = [self._normalize_run_id(run_id) for run_id in run_ids if run_id]
        return self._judgehost_state_store.case_cells_for_runs(safe_run_ids)

    def domjudge_solve_main_progress(self, verification_id: str) -> dict[str, int]:
        if not verification_id:
            return {"total": 0, "reported": 0}
        run_ids: list[str] = []
        with self._state_lock:
            for row in self._tasks_by_id.values():
                artifact_verification_id = row["artifact_verification_id"]
                if artifact_verification_id != verification_id:
                    continue
                run_id = row["run_id"]
                if (not run_id) or (not run_id.startswith("r-solve-main-")):
                    continue
                if run_id not in run_ids:
                    run_ids.append(run_id)
        return self._judgehost_state_store.aggregate_case_counts(run_ids)


    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        return self._judgehost_state_store.forget_runs(run_ids)
