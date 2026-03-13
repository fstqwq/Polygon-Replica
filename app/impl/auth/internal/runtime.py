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
    sandbox_name = _sanitize_runtime_profile_value(getattr(config.preview_sandbox_backend, "name", ""), "n/a")
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
    if safe_table not in {"builds", "previews", "verifications", "contest_jobs"}:
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
        summary_obj["status"] = "failed"
        summary_obj["finished_at"] = now_text
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
    inflight_entries: list[dict[str, str]] = []
    service = getattr(config, "judgehost_task_service", None)
    if service is not None:
        try:
            inflight_entries = list(service.startup_cancel_inflight_tasks(reason=reason))
        except Exception as exc:
            warnings.warn(f"startup judgehost inflight scan failed: {exc}", RuntimeWarning)
    if service is not None:
        try:
            service.cancel_all_domjudge_inflight()
        except Exception as exc:
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)
    if not inflight_entries:
        return
    try:
        from app.service.verification import (
            load_verification_run,
            load_verification_record,
            load_verification_summary,
            save_verification_run_summary,
        )
    except Exception as exc:
        warnings.warn(f"startup verification inflight import failed: {exc}", RuntimeWarning)
        return
    for item in inflight_entries:
        verification_id = str(item.get("verification_id") or "").strip()
        run_id = str(item.get("run_id") or "").strip()
        if not verification_id or not run_id:
            continue
        verification_row_raw = load_verification_record(config.db, verification_id)
        verification_row = dict(verification_row_raw) if verification_row_raw is not None else None
        if verification_row is None:
            continue
        status = str(verification_row.get("status") or "").strip().lower()
        if status not in {"running", "queued", "pending"}:
            continue
        verification_summary = load_verification_summary(config.db, verification_id)
        run_row = load_verification_run(
            config.db,
            verification_id=verification_id,
            run_id=run_id,
        )
        run_summary = run_row.get("summary") if isinstance(run_row, dict) else None
        summary_obj = dict(run_summary) if isinstance(run_summary, dict) else {}
        summary_obj["cancelled"] = True
        summary_obj["cancel_reason"] = reason
        if not str(summary_obj.get("error") or "").strip():
            summary_obj["error"] = reason
        build_id = str(summary_obj.get("build_id") or verification_row.get("build_id") or "").strip()
        source_label = str(summary_obj.get("source") or "").strip() or run_id
        source_paths_obj = verification_summary.get("source_paths")
        source_paths = list(source_paths_obj) if isinstance(source_paths_obj, list) else ([source_label] if source_label else [])
        try:
            save_verification_run_summary(
                config.db,
                config.fs_manager,
                verification_id=verification_id,
                problem_id=int(verification_row.get("problem_id") or 0),
                workspace_id=int(verification_row.get("workspace_id") or 0) if verification_row.get("workspace_id") is not None else None,
                build_id=build_id,
                kind=str(verification_row.get("kind") or "verification").strip() or "verification",
                mode=str(summary_obj.get("mode") or verification_summary.get("mode") or "pass-fail").strip() or "pass-fail",
                verification_source=str(verification_summary.get("verification_source") or "run.execute").strip() or "run.execute",
                source_paths=source_paths,
                run_id=run_id,
                run_status="failed",
                source_label=source_label,
                expected_behavior=str(run_row.get("expected_behavior") or summary_obj.get("expected_behavior") or "unknown").strip() or "unknown",
                run_summary=summary_obj,
                artifact_path=str(run_row.get("artifact_path") or "").strip(),
                error_text=str(summary_obj.get("error") or "").strip(),
                finished=True,
            )
        except Exception as exc:
            warnings.warn(
                f"startup verification cancel failed for {verification_id}/{run_id}: {exc}",
                RuntimeWarning,
            )


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
    _startup_cancel_summary_rows("contest_jobs", cancel_reason, now_text=now_text)
    _startup_cancel_judgehost_inflight(cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("verifications", cancel_reason, now_text=now_text)
    _startup_clear_all_caches()


def startup() -> None:
    config.db.init()
    _startup_reset_runtime_state()
    config.worker_queue_service.start()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)

