from __future__ import annotations

from .shared import (
    Path,
    datetime,
    domjudge_solve_main_progress,
    domjudge_case_progress_for_runs,
    json,
    logger,
    now_iso,
    now_iso_after,
    parse_iso_utc,
    time,
    timezone,
)
from app.service.verification import load_verification_run, save_verification_run_summary


class JudgehostQueueMixin:
    def _claim_lease_requeue_slot(self, *, interval_sec: float = 0.75) -> bool:
        now_mono = time.monotonic()
        with self._lease_requeue_lock:
            if now_mono < float(self._lease_requeue_next_ts):
                return False
            self._lease_requeue_next_ts = now_mono + max(0.05, float(interval_sec))
            return True

    def _requeue_expired_leases(self, conn=None, *, force: bool = False) -> None:
        if (not force) and (not self._claim_lease_requeue_slot()):
            return
        now_dt = datetime.now(timezone.utc)
        now_text = now_dt.isoformat()
        with self._state_lock:
            for task in self._tasks_by_id.values():
                if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
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
        conn=None,
        *,
        hostname: str,
        action: str,
        task_id: str = "",
        run_id: str = "",
        lease_expires_at: str = "",
    ) -> None:
        safe_host = self._normalize_hostname(hostname)
        safe_action = str(action or "").strip().lower() or "event"
        now_text = now_iso()
        with self._state_lock:
            row = self._hosts_state.get(safe_host)
            if row is None:
                row = {
                    "hostname": safe_host,
                    "enabled": True,
                    "first_seen_at": now_text,
                    "last_seen_at": now_text,
                    "last_action": "",
                    "last_task_id": "",
                    "last_run_id": "",
                    "lease_expires_at": "",
                    "update_count": 0,
                }
                self._hosts_state[safe_host] = row
            row["last_seen_at"] = now_text
            row["last_action"] = safe_action
            row["last_task_id"] = str(task_id or "").strip()
            row["last_run_id"] = str(run_id or "").strip()
            row["lease_expires_at"] = str(lease_expires_at or "").strip()
            row["update_count"] = int(row.get("update_count") or 0) + 1

    def _host_enabled_conn(self, conn=None, hostname: str = "") -> bool:
        safe_host = self._normalize_hostname(hostname)
        if not safe_host:
            return True
        with self._state_lock:
            row = self._hosts_state.get(safe_host)
            if row is None:
                return True
            return bool(row.get("enabled", True))

    def fetch_work(self, hostname: str, limit: int | None = None) -> list[dict[str, object]]:
        safe_host = self._normalize_hostname(hostname)
        cap = self._fetch_batch_size if limit is None else max(1, min(256, int(limit)))
        tasks: list[dict[str, object]] = []
        self._requeue_expired_leases()
        event_action = "fetch"
        event_task_id = ""
        event_run_id = ""
        event_lease_expires_at = ""
        with self._state_lock:
            host_row = self._hosts_state.get(safe_host)
            if host_row is not None and not bool(host_row.get("enabled", True)):
                event_action = "disabled"
            else:
                queued = [
                    dict(row)
                    for row in self._tasks_by_id.values()
                    if str(row.get("status") or "").strip().lower() == self.STATUS_QUEUED
                ]
                queued.sort(key=lambda item: str(item.get("created_at") or ""))
                lease_until = now_iso_after(self._lease_sec)
                now_text = now_iso()
                for row in queued[:cap]:
                    task_id = str(row.get("id") or "").strip()
                    task = self._tasks_by_id.get(task_id)
                    if task is None:
                        continue
                    if str(task.get("status") or "").strip().lower() != self.STATUS_QUEUED:
                        continue
                    task["status"] = self.STATUS_LEASED
                    task["lease_owner"] = safe_host
                    task["lease_expires_at"] = lease_until
                    task["updated_at"] = now_text
                    task["attempt_count"] = int(task.get("attempt_count") or 0) + 1
                    payload_obj = task.get("payload")
                    tasks.append(
                        {
                            "task_id": task_id,
                            "run_id": str(task.get("run_id") or ""),
                            "problem": str(task.get("problem_slug") or ""),
                            "username": str(task.get("username") or ""),
                            "artifact_verification_id": str(task.get("artifact_verification_id") or ""),
                            "mode": str(task.get("mode") or ""),
                            "lease_expires_at": lease_until,
                            "payload": dict(payload_obj) if isinstance(payload_obj, dict) else {},
                        }
                    )
                if tasks:
                    tail = tasks[-1]
                    event_action = "lease"
                    event_task_id = str(tail.get("task_id") or "")
                    event_run_id = str(tail.get("run_id") or "")
                    event_lease_expires_at = lease_until
        self._record_host_event_conn(
            hostname=safe_host,
            action=event_action,
            task_id=event_task_id,
            run_id=event_run_id,
            lease_expires_at=event_lease_expires_at,
        )
        return tasks

    def renew_lease(self, task_id: str, hostname: str) -> bool:
        safe_host = self._normalize_hostname(hostname)
        token = str(task_id or "").strip()
        if not token:
            return False
        with self._state_lock:
            task = self._tasks_by_id.get(token)
            if task is None:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            if str(task.get("lease_owner") or "").strip() != safe_host:
                self._record_host_event_conn(hostname=safe_host, action="heartbeat", task_id=token)
                return False
            now_text = now_iso()
            lease_until = now_iso_after(self._lease_sec)
            task["lease_expires_at"] = lease_until
            task["updated_at"] = now_text
            self._record_host_event_conn(
                hostname=safe_host,
                action="heartbeat",
                task_id=token,
                lease_expires_at=lease_until,
            )
            return True

    def _load_run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return {}
        safe_verification_id = str(verification_id or "").strip()
        if safe_verification_id:
            run_row = load_verification_run(
                self.db,
                verification_id=safe_verification_id,
                run_id=safe_run_id,
            )
            if isinstance(run_row, dict):
                summary_obj = run_row.get("summary")
                if isinstance(summary_obj, dict):
                    return dict(summary_obj)
        run_row = load_verification_run(
            self.db,
            verification_id=f"ver-{safe_run_id}",
            run_id=safe_run_id,
        )
        if isinstance(run_row, dict):
            summary_obj = run_row.get("summary")
            if isinstance(summary_obj, dict):
                return dict(summary_obj)
        row = None
        with self._state_lock:
            task_id = str(self._task_id_by_run.get(safe_run_id) or "").strip()
            if task_id:
                task_row = self._tasks_by_id.get(task_id)
                if isinstance(task_row, dict):
                    row = dict(task_row)
        if isinstance(row, dict):
            task_verification_id = str(row.get("verification_id") or "").strip()
            task_run_id = str(row.get("run_id") or "").strip() or safe_run_id
            if (
                task_run_id != safe_run_id
                or task_verification_id != safe_verification_id
            ):
                summary_obj = self._load_run_summary(task_run_id, task_verification_id)
                if summary_obj:
                    return summary_obj
            cached = row.get("summary")
            if isinstance(cached, dict):
                return dict(cached)
        return {}

    @staticmethod
    def _dict_or_empty(value: object) -> dict[str, object]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _summary_error_text(summary_obj: dict[str, object]) -> str:
        diagnostics_obj = summary_obj.get("compile_diagnostics")
        diagnostics = diagnostics_obj if isinstance(diagnostics_obj, list) else []
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if message:
                return message
        return str(summary_obj.get("error") or "").strip()

    def report_result(self, *, task_id: str, hostname: str, payload: dict[str, object]) -> dict[str, object]:
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            raise RuntimeError("task_id is required")
        safe_host = self._normalize_hostname(hostname)
        payload_obj = self._dict_or_empty(payload)
        run_status_raw = str(
            payload_obj.get("run_status")
            or payload_obj.get("status")
            or payload_obj.get("result")
            or ""
        ).strip().lower()
        if run_status_raw in {"ok", "accepted", "pass", "passed", "success", "completed"}:
            run_status = "ok"
            task_status = self.STATUS_COMPLETED
        else:
            run_status = "failed"
            task_status = self.STATUS_FAILED
        error_text = str(payload_obj.get("error") or "").strip()

        with self._state_lock:
            row = self._tasks_by_id.get(safe_task_id)
            if row is None:
                raise RuntimeError("judgehost task not found")
            current_status = str(row.get("status") or "").strip().lower()
            lease_owner = str(row.get("lease_owner") or "").strip()
            if current_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                raise RuntimeError(f"judgehost task is not reportable (status={current_status})")
            if lease_owner and lease_owner != safe_host:
                raise RuntimeError("judgehost task lease owner mismatch")
            run_id = str(row.get("run_id") or "").strip()
            verification_id = str(row.get("verification_id") or "").strip()
            run_id = str(row.get("run_id") or "").strip() or run_id
            prev_status = current_status
            prev_lease_owner = lease_owner
            prev_lease_expires_at = str(row.get("lease_expires_at") or "").strip()
            prev_error_text = str(row.get("error_text") or "").strip()
            prev_result = self._dict_or_empty(row.get("result"))
            prev_summary = self._dict_or_empty(row.get("summary"))
            persist_verification_run = bool(row.get("persist_verification_run", True))
            row["status"] = self.STATUS_REPORTING
            row["updated_at"] = now_iso()

        try:
            existing_summary = self._load_run_summary(run_id, verification_id)
            if not existing_summary:
                existing_summary = self._dict_or_empty(prev_summary)
            summary = payload_obj.get("summary")
            if isinstance(summary, dict):
                summary_obj: dict[str, object] = dict(summary)
            else:
                summary_obj = self._dict_or_empty(existing_summary)
            if "tests" not in summary_obj:
                summary_obj["tests"] = list(existing_summary.get("tests") or [])
            if "source" not in summary_obj:
                summary_obj["source"] = str(existing_summary.get("source") or "upload")
            if "mode" not in summary_obj:
                summary_obj["mode"] = str(existing_summary.get("mode") or "pass-fail")
            if "limits" not in summary_obj:
                summary_obj["limits"] = self._dict_or_empty(existing_summary.get("limits"))
            if "usage" not in summary_obj:
                summary_obj["usage"] = self._dict_or_empty(existing_summary.get("usage"))
            if run_status != "ok":
                if error_text:
                    summary_obj["error"] = error_text
                elif "error" not in summary_obj:
                    summary_obj["error"] = "judgehost reported failure"
            summary_obj["status"] = run_status

            judgehost_block = self._dict_or_empty(summary_obj.get("judgehost"))
            judgehost_block["task_id"] = safe_task_id
            judgehost_block["hostname"] = safe_host
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
                row = self._tasks_by_id.get(safe_task_id)
                if row is not None and str(row.get("status") or "").strip().lower() == self.STATUS_REPORTING:
                    row["status"] = prev_status
                    row["lease_owner"] = prev_lease_owner
                    row["lease_expires_at"] = prev_lease_expires_at
                    row["error_text"] = prev_error_text
                    row["result"] = self._dict_or_empty(prev_result)
                    row["summary"] = self._dict_or_empty(prev_summary)
                    row["updated_at"] = now_iso()
            raise

        with self._state_lock:
            row = self._tasks_by_id.get(safe_task_id)
            if row is not None:
                row["status"] = task_status
                row["result"] = self._dict_or_empty(payload_obj)
                row["summary"] = self._dict_or_empty(summary_obj)
                row["run_status"] = run_status
                row["error_text"] = error_text
                row["lease_owner"] = safe_host
                row["lease_expires_at"] = ""
                row["updated_at"] = finished_at
                row["completed_at"] = finished_at
        self._record_host_event_conn(
            hostname=safe_host,
            action="report",
            task_id=safe_task_id,
            run_id=run_id,
        )
        run_artifact_path = ""
        if persist_verification_run and verification_id and run_id:
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
                run_artifact_path = ""
        return {
            "task_id": safe_task_id,
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
        safe_verification_id = str(verification_id or "").strip()
        safe_run_id = str(run_id or "").strip()
        if not safe_verification_id or not safe_run_id:
            return
        problem_slug = str(row.get("problem_slug") or "").strip()
        username = str(row.get("username") or "").strip()
        if (not problem_slug) or (not username):
            return
        ctx = self._workspace_service.workspace_context(problem_slug, username, include_recent=False)
        run_root = self._fs_manager.prepare_verification_run_root(safe_verification_id, safe_run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        payload_obj = row.get("payload")
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        verification_source = str(payload.get("verification_source") or "").strip() or "run.execute"
        save_verification_run_summary(
            self.db,
            self._fs_manager,
            verification_id=safe_verification_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit="",
            source_ref="",
            kind="verification",
            mode=str(row.get("mode") or "").strip() or "pass-fail",
            verification_source=verification_source,
            source_paths=[str(summary_obj.get("source") or "").strip()] if str(summary_obj.get("source") or "").strip() else [],
            run_id=safe_run_id,
            run_status=run_status,
            source_label=str(summary_obj.get("source") or "").strip() or safe_run_id,
            expected_behavior=str(payload.get("expected_behavior") or "unknown").strip() or "unknown",
            run_summary=summary_obj,
            artifact_path=str(run_root),
            task_kind=str(payload.get("task_kind") or "").strip(),
            error_text=str(error_text or "").strip(),
        )

    def wait_for_task_result(self, task_id: str, timeout_sec: float | None = None) -> dict[str, object]:
        safe_task_id = str(task_id or "").strip()
        if not safe_task_id:
            raise RuntimeError("judgehost task id is required")
        timeout = self._wait_timeout_sec if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        while True:
            row = self._task_by_id(safe_task_id)
            if row is None:
                raise RuntimeError("judgehost task disappeared")
            status = str(row.get("status") or "").strip().lower()
            if status in {self.STATUS_COMPLETED, self.STATUS_FAILED}:
                verification_id = str(row.get("verification_id") or "").strip()
                run_id = str(row.get("run_id") or "").strip()
                artifact_path = ""
                if verification_id and run_id:
                    try:
                        artifact_path = str(
                            self._fs_manager.prepare_verification_run_root(
                                verification_id,
                                run_id,
                            ).resolve()
                        )
                    except Exception:
                        artifact_path = ""
                return {
                    "task_id": safe_task_id,
                    "verification_id": verification_id,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "status": str(row.get("run_status") or status).strip().lower(),
                    "task_status": status,
                    "error": str(row.get("error_text") or "").strip(),
                    "summary": self._dict_or_empty(row.get("summary")),
                }
            if time.monotonic() >= deadline:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            time.sleep(self._wait_poll_sec)

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        result = self.wait_for_task_result(task_id, timeout_sec=timeout_sec)
        task_status = str(result.get("task_status") or "").strip().lower()
        if task_status == self.STATUS_FAILED:
            detail = str(result.get("error") or "").strip() or "judgehost task failed"
            raise RuntimeError(detail)
        return str(result.get("run_id") or "").strip()

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
                if str(task.get("status") or "").strip().lower() != self.STATUS_LEASED:
                    continue
                host = self._normalize_hostname(str(task.get("lease_owner") or "").strip())
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
                key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get("hostname") or "")),
                reverse=True,
            )
        rows_out: list[dict[str, object]] = []
        online_count = 0
        for row in host_rows:
            hostname = self._normalize_hostname(str(row.get("hostname") or "").strip())
            if not hostname:
                continue
            enabled_flag = bool(row.get("enabled", True))
            last_seen = str(row.get("last_seen_at") or "").strip()
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
            rows_out.append(
                {
                    "hostname": hostname,
                    "enabled": enabled_flag,
                    "online": is_online,
                    "age_sec": age_sec,
                    "last_seen_at": last_seen,
                    "first_seen_at": str(row.get("first_seen_at") or "").strip(),
                    "last_action": str(row.get("last_action") or "").strip(),
                    "last_task_id": str(row.get("last_task_id") or "").strip(),
                    "last_run_id": str(row.get("last_run_id") or "").strip(),
                    "lease_expires_at": str(row.get("lease_expires_at") or "").strip(),
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
                    "last_judging": str(last_judging.get("label") or "-"),
                    "last_judging_at": str(last_judging.get("updated_at") or ""),
                }
            )
        return rows_out, online_count

    def set_host_enabled(self, hostname: str, enabled: bool) -> dict[str, int]:
        safe_host = self._normalize_hostname(hostname)
        now_text = now_iso()
        safe_enabled = bool(enabled)
        released_tasks = 0
        with self._state_lock:
            row = self._host_state_row(safe_host)
            row["enabled"] = safe_enabled
            row["last_seen_at"] = now_text
            row["last_action"] = "enabled" if safe_enabled else "disabled"
            row["update_count"] = int(row.get("update_count") or 0) + 1
            if not safe_enabled:
                for task in self._tasks_by_id.values():
                    if str(task.get("lease_owner") or "").strip() != safe_host:
                        continue
                    task_status = str(task.get("status") or "").strip().lower()
                    if task_status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                        continue
                    task["status"] = self.STATUS_QUEUED
                    task["lease_owner"] = ""
                    task["lease_expires_at"] = ""
                    task["updated_at"] = now_text
                    released_tasks += 1
        released_jobs = 0
        released_cases = 0
        if not safe_enabled:
            with self._domdb_conn() as conn:
                job_upd = conn.execute(
                    """
                    UPDATE judgehost_domjudge_jobs
                    SET lease_owner=NULL, status='queued', updated_at=?
                    WHERE lease_owner=? AND status IN ('leased','queued')
                    """,
                    [now_text, safe_host],
                )
                released_jobs = int(job_upd.rowcount or 0)
                case_upd = conn.execute(
                    """
                    UPDATE judgehost_domjudge_cases
                    SET status='pending', lease_owner=NULL, updated_at=?
                    WHERE lease_owner=? AND status='leased'
                    """,
                    [now_text, safe_host],
                )
                released_cases = int(case_upd.rowcount or 0)
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
        safe_reason = str(reason or "").strip() or "verification cancelled by user"
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        now_text = now_iso()
        affected = 0
        with self._state_lock:
            for run_id in safe_ids:
                task_id = str(self._task_id_by_run.get(run_id) or "").strip()
                if not task_id:
                    continue
                row = self._tasks_by_id.get(task_id)
                if row is None:
                    continue
                status = str(row.get("status") or "").strip().lower()
                if status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": safe_reason, "error": safe_reason}
                row["error_text"] = safe_reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
                affected += 1
        return affected

    def active_task_count_for_verification(self, verification_id: str) -> int:
        safe_verification_id = str(verification_id or "").strip()
        if not safe_verification_id:
            return 0
        count = 0
        with self._state_lock:
            for row in self._tasks_by_id.values():
                if str(row.get("artifact_verification_id") or "").strip() != safe_verification_id:
                    continue
                status = str(row.get("status") or "").strip().lower()
                if status in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    count += 1
        return count

    def startup_cancel_inflight_tasks(self, *, reason: str) -> list[dict[str, str]]:
        safe_reason = str(reason or "").strip() or "startup reset"
        now_text = now_iso()
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        with self._state_lock:
            for row in self._tasks_by_id.values():
                status = str(row.get("status") or "").strip().lower()
                if status not in {self.STATUS_QUEUED, self.STATUS_LEASED}:
                    continue
                run_id = str(row.get("run_id") or "").strip()
                verification_id = str(row.get("verification_id") or "").strip()
                run_id = str(row.get("run_id") or "").strip() or run_id
                entry_key = (verification_id, run_id)
                if verification_id and run_id and entry_key not in seen:
                    seen.add(entry_key)
                    entries.append(
                        {
                            "run_id": run_id,
                            "verification_id": verification_id,
                            "run_id": run_id,
                        }
                    )
                row["status"] = self.STATUS_FAILED
                row["result"] = {"cancelled": True, "reason": safe_reason, "error": safe_reason}
                row["error_text"] = safe_reason
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                row["completed_at"] = now_text
        return entries

    def forget_problem_tasks(self, problem_slug: str) -> int:
        safe_problem = str(problem_slug or "").strip()
        if not safe_problem:
            return 0
        removed = 0
        with self._state_lock:
            remove_ids = [
                task_id
                for task_id, row in self._tasks_by_id.items()
                if str(row.get("problem_slug") or "").strip() == safe_problem
            ]
            for task_id in remove_ids:
                row = self._tasks_by_id.pop(task_id, None)
                if row is None:
                    continue
                run_id = str(row.get("run_id") or "").strip()
                if run_id and self._task_id_by_run.get(run_id) == task_id:
                    self._task_id_by_run.pop(run_id, None)
                removed += 1
        return removed

    def cancel_domjudge_jobs_for_runs(self, run_ids: list[str], *, final_status: str = "failed") -> int:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_ids))
        now_text = now_iso()
        with self._domdb_conn() as conn:
            job_rows = conn.execute(
                f"SELECT job_id FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders}) AND status IN ('queued','leased')",
                [*safe_ids],
            ).fetchall()
            job_ids = [int(row["job_id"]) for row in job_rows if row is not None and row["job_id"] is not None]
            if not job_ids:
                return 0
            jph = ",".join(("?" for _ in job_ids))
            conn.execute(
                f"""
                UPDATE judgehost_domjudge_cases
                SET status='reported',
                    lease_owner=NULL,
                    runresult=CASE WHEN runresult IS NULL OR TRIM(runresult)='' THEN 'internal-error' ELSE runresult END,
                    runtime_sec=COALESCE(runtime_sec, 0),
                    cpu_sec=COALESCE(cpu_sec, 0),
                    wall_sec=COALESCE(wall_sec, 0),
                    memory_kb=COALESCE(memory_kb, 0),
                    updated_at=?
                WHERE job_id IN ({jph}) AND status IN ('pending','leased')
                """,
                [now_text, *job_ids],
            )
            conn.execute(
                f"""
                UPDATE judgehost_domjudge_jobs
                SET status=?,
                    lease_owner=NULL,
                    completed_at=COALESCE(completed_at, ?),
                    updated_at=?
                WHERE job_id IN ({jph}) AND status IN ('queued','leased')
                """,
                [str(final_status or "failed"), now_text, now_text, *job_ids],
            )
            return len(job_ids)

    def cancel_all_domjudge_inflight(self) -> int:
        now_text = now_iso()
        with self._domdb_conn() as conn:
            conn.execute(
                """
                UPDATE judgehost_domjudge_jobs
                SET status='failed',
                    lease_owner=NULL,
                    updated_at=?,
                    completed_at=COALESCE(completed_at, ?)
                WHERE status IN ('queued','leased')
                """,
                [now_text, now_text],
            )
            case_upd = conn.execute(
                """
                UPDATE judgehost_domjudge_cases
                SET status='reported',
                    lease_owner=NULL,
                    runresult=CASE WHEN runresult IS NULL OR TRIM(runresult)='' THEN 'internal-error' ELSE runresult END,
                    updated_at=?
                WHERE status IN ('pending','leased')
                """,
                [now_text],
            )
            try:
                return int(case_upd.rowcount or 0)
            except Exception:
                return 0

    def domjudge_case_progress_for_runs(self, run_ids: list[str]) -> dict[str, dict[str, int]]:
        return domjudge_case_progress_for_runs(
            normalize_run_id=self._normalize_run_id,
            db_fetch_all=self._db_fetch_all,
            run_ids=run_ids,
        )

    def domjudge_case_cells_for_runs(self, run_ids: list[str]) -> list[dict[str, object]]:
        safe_ids: list[str] = []
        for item in list(run_ids or []):
            token = self._domjudge_text(item)
            if token:
                safe_ids.append(self._normalize_run_id(token))
        if not safe_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_ids))
        rows = self._db_fetch_all(
            f"""
            SELECT j.run_id AS run_id,
                   c.test_name AS test_name,
                   c.status AS status,
                   c.runresult AS runresult,
                   c.cpu_sec AS cpu_sec,
                   c.runtime_sec AS runtime_sec,
                   c.wall_sec AS wall_sec,
                   c.memory_kb AS memory_kb
            FROM judgehost_domjudge_jobs j
            JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
            WHERE j.run_id IN ({placeholders})
            ORDER BY j.run_id ASC, c.ordinal ASC, c.id ASC
            """,
            [*safe_ids],
        )
        return [dict(row) for row in rows]

    def domjudge_solve_main_progress(self, verification_id: str) -> dict[str, int]:
        return domjudge_solve_main_progress(
            state_lock=self._state_lock,
            tasks_by_id=self._tasks_by_id,
            db_fetch_one=self._db_fetch_one,
            artifact_verification_id=verification_id,
        )

    def forget_domjudge_runs(self, run_ids: list[str]) -> int:
        safe_ids = [self._normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
        if not safe_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_ids))
        with self._domdb_conn() as conn:
            conn.execute(
                f"DELETE FROM judgehost_domjudge_cases WHERE run_id IN ({placeholders})",
                [*safe_ids],
            )
            cur = conn.execute(
                f"DELETE FROM judgehost_domjudge_jobs WHERE run_id IN ({placeholders})",
                [*safe_ids],
            )
            try:
                return int(cur.rowcount or 0)
            except Exception:
                return 0
