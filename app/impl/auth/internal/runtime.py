from __future__ import annotations

import json
import shutil
import time
import warnings
from pathlib import Path

from app.db import now_iso
from app.impl.runtime.config import config

_C = config.constants

_RUNTIME_BACKEND_CACHE: dict[str, str] | None = None
_RUNTIME_BACKEND_CACHE_TS = 0.0
_RUNTIME_PROFILE_MAX_LEN = 160
_RUNTIME_BACKEND_CACHE_TTL_SEC = 2.0


def _sanitize_runtime_profile_value(raw: object, default: str = "n/a") -> str:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return default
    if len(text) > _RUNTIME_PROFILE_MAX_LEN:
        return text[: _RUNTIME_PROFILE_MAX_LEN - 3].rstrip() + "..."
    return text

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
    if safe_table not in {"previews", "verifications", "contest_jobs"}:
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
        if not summary_obj.get("error"):
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
    from app.service.verification.store import (
        load_verification_run,
        load_verification_record,
        load_verification_summary,
        save_verification_run_summary,
    )
    for item in inflight_entries:
        verification_id_raw = item.get("verification_id")
        run_id_raw = item.get("run_id")
        verification_id = verification_id_raw.strip() if isinstance(verification_id_raw, str) else ""
        run_id = run_id_raw.strip() if isinstance(run_id_raw, str) else ""
        if not verification_id or not run_id:
            continue
        verification_row_raw = load_verification_record(config.db, verification_id)
        verification_row = dict(verification_row_raw) if verification_row_raw is not None else None
        if verification_row is None:
            continue
        status_raw = verification_row.get("status")
        status = status_raw.strip().lower() if isinstance(status_raw, str) else ""
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
        summary_error = summary_obj.get("error")
        if not (summary_error.strip() if isinstance(summary_error, str) else ""):
            summary_obj["error"] = reason
        source_raw = summary_obj.get("source")
        source_label = source_raw.strip() if isinstance(source_raw, str) and source_raw.strip() else run_id
        source_paths_obj = verification_summary.get("source_paths")
        source_paths = list(source_paths_obj) if isinstance(source_paths_obj, list) else ([source_label] if source_label else [])
        kind_raw = verification_row.get("kind")
        kind = kind_raw.strip() if isinstance(kind_raw, str) and kind_raw.strip() else "verification"
        mode_raw = summary_obj.get("mode")
        verification_mode_raw = verification_summary.get("mode")
        mode = (
            mode_raw.strip()
            if isinstance(mode_raw, str) and mode_raw.strip()
            else verification_mode_raw.strip()
            if isinstance(verification_mode_raw, str) and verification_mode_raw.strip()
            else "pass-fail"
        )
        verification_source_raw = verification_summary.get("verification_source")
        verification_source = (
            verification_source_raw.strip()
            if isinstance(verification_source_raw, str) and verification_source_raw.strip()
            else "run.execute"
        )
        run_expected_raw = run_row.get("expected_behavior") if isinstance(run_row, dict) else None
        summary_expected_raw = summary_obj.get("expected_behavior")
        expected_behavior = (
            run_expected_raw.strip()
            if isinstance(run_expected_raw, str) and run_expected_raw.strip()
            else summary_expected_raw.strip()
            if isinstance(summary_expected_raw, str) and summary_expected_raw.strip()
            else "unknown"
        )
        artifact_path_raw = run_row.get("artifact_path") if isinstance(run_row, dict) else None
        artifact_path = artifact_path_raw.strip() if isinstance(artifact_path_raw, str) else ""
        error_text_raw = summary_obj.get("error")
        error_text = error_text_raw.strip() if isinstance(error_text_raw, str) else ""
        try:
            problem_id_value = verification_row.get("problem_id")
            if not isinstance(problem_id_value, int):
                raise RuntimeError("verification row missing problem_id")
            workspace_id_value = verification_row.get("workspace_id")
            if workspace_id_value is not None and not isinstance(workspace_id_value, int):
                raise RuntimeError("verification row has invalid workspace_id")
            save_verification_run_summary(
                config.db,
                config.fs_manager,
                verification_id=verification_id,
                problem_id=problem_id_value,
                workspace_id=workspace_id_value,
                kind=kind,
                mode=mode,
                verification_source=verification_source,
                source_paths=source_paths,
                run_id=run_id,
                run_status="failed",
                source_label=source_label,
                expected_behavior=expected_behavior,
                run_summary=summary_obj,
                artifact_path=artifact_path,
                error_text=error_text,
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
