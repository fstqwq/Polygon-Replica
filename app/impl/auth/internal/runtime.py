from __future__ import annotations

from app.impl.auth.internal.dependency import (
    Path,
    _C,
    _RUNTIME_BACKEND_CACHE_TTL_SEC,
    _RUNTIME_PROFILE_MAX_LEN,
    config,
    json,
    now_iso,
    platform,
    re,
    shutil,
    time,
    warnings,
)

_RUNTIME_PROFILE_CACHE: dict[str, str] | None = None
_RUNTIME_BACKEND_CACHE: dict[str, str] | None = None
_RUNTIME_BACKEND_CACHE_TS = 0.0


def _sanitize_runtime_profile_value(raw: object, default: str = "n/a") -> str:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return default
    if len(text) > _RUNTIME_PROFILE_MAX_LEN:
        return text[: _RUNTIME_PROFILE_MAX_LEN - 3].rstrip() + "..."
    return text


def _read_linux_distro_label() -> str:
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as fh:
            values: dict[str, str] = {}
            for line in fh:
                raw = str(line or "").strip()
                if not raw or "=" not in raw or raw.startswith("#"):
                    continue
                key, value = raw.split("=", 1)
                values[str(key or "").strip()] = str(value or "").strip().strip('"').strip("'")
    except Exception:
        values = {}
    return str(values.get("PRETTY_NAME") or values.get("NAME") or "").strip()


def _parse_cpu_frequency_ghz(raw: object) -> float | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    match_ghz = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*ghz", text)
    if match_ghz is not None:
        try:
            value = float(match_ghz.group(1))
            if value > 0:
                return value
        except Exception:
            return None
    match_mhz = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mhz", text)
    if match_mhz is not None:
        try:
            value = float(match_mhz.group(1)) / 1000.0
            if value > 0:
                return value
        except Exception:
            return None
    try:
        numeric = float(text)
    except Exception:
        return None
    if numeric > 100:
        return numeric / 1000.0
    if numeric > 0:
        return numeric
    return None


def _format_cpu_label(label: str, freq_ghz: float | None) -> str:
    text = " ".join(str(label or "").split()).strip()
    if not text:
        return ""
    if freq_ghz is None:
        freq_ghz = _parse_cpu_frequency_ghz(text)
    if freq_ghz is None or freq_ghz <= 0:
        return text
    suffix = f" @{freq_ghz:.2f}GHz"
    if re.search(r"@\s*[0-9]+(?:\.[0-9]+)?\s*ghz", text, flags=re.IGNORECASE):
        return re.sub(r"@\s*[0-9]+(?:\.[0-9]+)?\s*ghz", suffix, text, count=1, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[0-9]+(?:\.[0-9]+)?\s*ghz\b", "", text, flags=re.IGNORECASE).strip()
    if not cleaned:
        cleaned = text
    return f"{cleaned}{suffix}"


def _read_cpu_info_details() -> tuple[str, float | None]:
    try:
        primary = ""
        secondary = ""
        freq_ghz: float | None = None
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = str(line or "")
                if ":" not in text:
                    continue
                key, value = text.split(":", 1)
                k = str(key or "").strip().lower()
                cleaned = str(value or "").strip()
                if not cleaned:
                    continue
                if k in {"model name", "cpu model", "hardware"}:
                    if (not primary) and (not re.fullmatch(r"\d+", cleaned)):
                        primary = cleaned
                    continue
                if k == "processor" and (not secondary) and (not re.fullmatch(r"\d+", cleaned)):
                    secondary = cleaned
                    continue
                if (k in {"cpu mhz", "clock"}) and (freq_ghz is None):
                    freq_ghz = _parse_cpu_frequency_ghz(cleaned)
        if primary:
            return (primary, freq_ghz)
        if secondary:
            return (secondary, freq_ghz)
    except Exception:
        pass
    return ("", None)


def _runtime_footer_profile() -> dict[str, str]:
    global _RUNTIME_PROFILE_CACHE
    if isinstance(_RUNTIME_PROFILE_CACHE, dict):
        return dict(_RUNTIME_PROFILE_CACHE)
    distro = _read_linux_distro_label()
    if not distro:
        distro = f"{platform.system()} {platform.release()}".strip()
    cpu_name, cpu_ghz = _read_cpu_info_details()
    if not cpu_name:
        cpu_name = str(platform.processor() or platform.machine() or "").strip()
    cpu = _format_cpu_label(cpu_name, cpu_ghz)
    profile = {
        "runtime_linux_distro": _sanitize_runtime_profile_value(distro),
        "runtime_cpu_info": _sanitize_runtime_profile_value(cpu),
    }
    _RUNTIME_PROFILE_CACHE = dict(profile)
    return profile


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _runtime_backend_profile() -> dict[str, str]:
    global _RUNTIME_BACKEND_CACHE, _RUNTIME_BACKEND_CACHE_TS
    now = time.monotonic()
    if (
        isinstance(_RUNTIME_BACKEND_CACHE, dict)
        and (now - float(_RUNTIME_BACKEND_CACHE_TS)) <= _RUNTIME_BACKEND_CACHE_TTL_SEC
    ):
        return dict(_RUNTIME_BACKEND_CACHE)
    sandbox_name = _sanitize_runtime_profile_value(getattr(config.sandbox_backend, "name", ""), "n/a")
    sandbox_count = "1" if sandbox_name != "n/a" else "0"
    judgehost_enabled = False
    hosts_online = 0
    hosts_total = 0
    queued = 0
    leased = 0
    completed = 0
    failed = 0
    try:
        status = config.judgehost_task_service.status()
        if isinstance(status, dict):
            judgehost_enabled = bool(status.get("enabled"))
            hosts_online = _safe_int(status.get("hosts_online"), 0)
            hosts_total = _safe_int(status.get("hosts_total"), 0)
            queue = status.get("queue")
            if isinstance(queue, dict):
                queued = _safe_int(queue.get("queued"), 0)
                leased = _safe_int(queue.get("leased"), 0)
                completed = _safe_int(queue.get("completed"), 0)
                failed = _safe_int(queue.get("failed"), 0)
    except Exception:
        pass
    hosts_online = max(0, int(hosts_online))
    hosts_total = max(0, int(hosts_total))
    queued = max(0, int(queued))
    leased = max(0, int(leased))
    completed = max(0, int(completed))
    failed = max(0, int(failed))
    if judgehost_enabled:
        judgehost_summary = (
            f"online {hosts_online}/{hosts_total}; "
            f"queued={queued}; leased={leased}; completed={completed}; failed={failed}"
        )
    else:
        judgehost_summary = "disabled"
    judgehost_danger = (not judgehost_enabled) or (hosts_online <= 0)
    profile = {
        "runtime_sandbox_backend": sandbox_name,
        "runtime_sandbox_backend_count": sandbox_count,
        "runtime_judgehost_backend_summary": _sanitize_runtime_profile_value(judgehost_summary),
        "runtime_judgehost_backend_danger": "1" if judgehost_danger else "0",
        "runtime_judgehost_enabled": "1" if judgehost_enabled else "0",
        "runtime_judgehost_hosts_online": str(hosts_online),
        "runtime_judgehost_hosts_total": str(hosts_total),
    }
    _RUNTIME_BACKEND_CACHE = dict(profile)
    _RUNTIME_BACKEND_CACHE_TS = now
    return profile


def _startup_cancel_summary_rows(table_name: str, reason: str, *, now_text: str) -> None:
    safe_table = str(table_name or "").strip()
    if safe_table not in {"builds", "previews", "runs", "contest_jobs"}:
        return
    try:
        rows = config.db.fetch_all(
            f"SELECT id,summary_json FROM {safe_table} WHERE status IN ('running','queued','pending')"
        )
    except Exception as exc:
        warnings.warn(f"startup {safe_table} inflight scan failed: {exc}", RuntimeWarning)
        return
    for row in rows:
        row_id = str(row["id"] or "").strip()
        if not row_id:
            continue
        summary_obj: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["summary_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                summary_obj = dict(parsed)
        except Exception:
            summary_obj = {}
        summary_obj["cancelled"] = True
        summary_obj["cancel_reason"] = reason
        if not str(summary_obj.get("error") or "").strip():
            summary_obj["error"] = reason
        try:
            config.db.execute(
                f"""
                UPDATE {safe_table}
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=?
                """,
                [json.dumps(summary_obj), now_text, row_id],
            )
        except Exception as exc:
            warnings.warn(f"startup {safe_table} inflight cancel failed for {row_id}: {exc}", RuntimeWarning)


def _startup_cancel_judgehost_inflight(reason: str, *, now_text: str) -> None:
    run_ids: list[str] = []
    service = getattr(config, "judgehost_task_service", None)
    if service is not None:
        try:
            run_ids = list(service.startup_cancel_inflight_tasks(reason=reason))
        except Exception as exc:
            warnings.warn(f"startup judgehost inflight scan failed: {exc}", RuntimeWarning)
    if service is not None:
        try:
            service.cancel_all_domjudge_inflight()
        except Exception as exc:
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)
    if not run_ids:
        return
    placeholders = ",".join(("?" for _ in run_ids))
    try:
        run_rows = config.db.fetch_all(
            f"SELECT id,summary_json,status FROM runs WHERE id IN ({placeholders})",
            [*run_ids],
        )
    except Exception as exc:
        warnings.warn(f"startup judgehost run scan failed: {exc}", RuntimeWarning)
        return
    for run_row in run_rows:
        run_id = str(run_row["id"] or "").strip()
        if not run_id:
            continue
        status = str(run_row["status"] or "").strip().lower()
        if status not in {"running", "queued", "pending"}:
            continue
        summary_obj: dict[str, object] = {}
        try:
            parsed = json.loads(str(run_row["summary_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                summary_obj = dict(parsed)
        except Exception:
            summary_obj = {}
        summary_obj["cancelled"] = True
        summary_obj["cancel_reason"] = reason
        if not str(summary_obj.get("error") or "").strip():
            summary_obj["error"] = reason
        try:
            config.db.execute(
                """
                UPDATE runs
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=?
                """,
                [json.dumps(summary_obj), now_text, run_id],
            )
        except Exception as exc:
            warnings.warn(f"startup run cancel failed for {run_id}: {exc}", RuntimeWarning)


def _startup_normalize_run_token(raw: object) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    token = token.split("?", 1)[0].split("#", 1)[0].strip()
    if not token:
        return ""
    if "/" in token:
        token = token.rsplit("/", 1)[-1].strip()
    return token


def _startup_collect_invocation_run_ids(details: dict[str, object]) -> list[str]:
    values: list[str] = []
    primary = _startup_normalize_run_token(details.get("run_id"))
    if primary:
        values.append(primary)
    raw_run_ids = details.get("run_ids")
    if isinstance(raw_run_ids, list):
        for raw in raw_run_ids:
            token = _startup_normalize_run_token(raw)
            if token:
                values.append(token)
    seen: set[str] = set()
    deduped: list[str] = []
    for token in values:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _startup_cancel_audit_inflight(reason: str, *, now_text: str) -> None:
    try:
        rows = config.db.fetch_all(
            """
            SELECT id,actor_user_id,problem_id,action,details_json
            FROM audit_log
            WHERE action IN ('run.execute', 'verification.start', 'run.cancel')
            ORDER BY created_at DESC, id DESC
            """
        )
    except Exception as exc:
        warnings.warn(f"startup audit inflight scan failed: {exc}", RuntimeWarning)
        return
    seen: set[tuple[int, int, str]] = set()
    pending_cancel_rows: list[tuple[int | None, int | None, dict[str, object]]] = []
    for row in rows:
        details: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["details_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                details = dict(parsed)
        except Exception:
            details = {}
        invocation_id = _startup_normalize_run_token(details.get("invocation_id"))
        if not invocation_id:
            continue
        try:
            actor_user_id = int(row["actor_user_id"]) if row["actor_user_id"] is not None else None
        except Exception:
            actor_user_id = None
        try:
            problem_id = int(row["problem_id"]) if row["problem_id"] is not None else None
        except Exception:
            problem_id = None
        scope_key = (int(problem_id or -1), int(actor_user_id or -1), invocation_id)
        if scope_key in seen:
            continue
        seen.add(scope_key)
        action_token = str(row["action"] or "").strip().lower()
        if action_token == "run.cancel":
            continue
        status_token = str(details.get("status") or "").strip().lower()
        if status_token not in {"running", "queued", "pending"}:
            continue
        invocation_run_ids = _startup_collect_invocation_run_ids(details)
        cancel_details: dict[str, object] = {
            "invocation_id": invocation_id,
            "run_ids": invocation_run_ids,
            "run_count": len(invocation_run_ids),
            "cancelled_runs": 0,
            "cancelled_tasks": 0,
            "cancelled_builds": 0,
            "reason": reason,
        }
        pending_cancel_rows.append((actor_user_id, problem_id, cancel_details))
    for actor_user_id, problem_id, cancel_details in pending_cancel_rows:
        try:
            config.db.execute(
                """
                INSERT INTO audit_log(actor_user_id, problem_id, action, details_json, created_at)
                VALUES(?, ?, 'run.cancel', ?, ?)
                """,
                [
                    actor_user_id,
                    problem_id,
                    json.dumps(cancel_details),
                    now_text,
                ],
            )
        except Exception as exc:
            invocation_id = str(cancel_details.get("invocation_id") or "").strip()
            warnings.warn(f"startup audit inflight cancel failed for {invocation_id}: {exc}", RuntimeWarning)


def _startup_clear_all_caches() -> None:
    try:
        config.async_task_cache_service.clear_all()
    except Exception as exc:
        warnings.warn(f"startup async cache clear failed: {exc}", RuntimeWarning)
    try:
        config.judge_fs_index_service.clear_all()
    except Exception as exc:
        warnings.warn(f"startup judge fs index clear failed: {exc}", RuntimeWarning)
    testcase_cache_root = (config.settings.cache_root / "judgehost-domjudge-testcases").resolve()
    try:
        if testcase_cache_root.exists() and testcase_cache_root.is_dir() and (not testcase_cache_root.is_symlink()):
            shutil.rmtree(testcase_cache_root, ignore_errors=True)
    except Exception as exc:
        warnings.warn(f"startup testcase cache clear failed: {exc}", RuntimeWarning)
    try:
        testcase_cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        config.judgehost_task_service.clear_testcase_registry()
    except Exception as exc:
        warnings.warn(f"startup testcase registry reset failed: {exc}", RuntimeWarning)
    durable_log_raw = str(_C.WORKER_QUEUE_DURABLE_LOG or "").strip()
    durable_log = (config.settings.cache_root / "worker-queue-events.jsonl").resolve()
    if durable_log_raw:
        durable_log = Path(durable_log_raw).expanduser().resolve()
    try:
        durable_log.unlink(missing_ok=True)
    except Exception as exc:
        warnings.warn(f"startup worker queue durable log clear failed: {exc}", RuntimeWarning)


def _startup_reset_runtime_state() -> None:
    now_text = now_iso()
    cancel_reason = "cancelled on service startup"
    _startup_cancel_summary_rows("builds", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("previews", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("runs", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("contest_jobs", cancel_reason, now_text=now_text)
    _startup_cancel_judgehost_inflight(cancel_reason, now_text=now_text)
    _startup_cancel_audit_inflight(cancel_reason, now_text=now_text)
    _startup_clear_all_caches()


def startup() -> None:
    config.db.init()
    _startup_reset_runtime_state()
    config.invocation_backend_service.refresh()
    config.worker_queue_service.start()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)


