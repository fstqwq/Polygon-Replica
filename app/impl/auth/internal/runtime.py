from __future__ import annotations

import shutil
import time
import warnings
from pathlib import Path

from app.db import now_iso
from app.impl.runtime.config import config
from app.service.verification.types import is_cancel_reason

_C = config.constants

_RUNTIME_JUDGEHOST_HEALTH_CACHE: dict[str, str] | None = None
_RUNTIME_JUDGEHOST_HEALTH_CACHE_TS = 0.0
_RUNTIME_PROFILE_MAX_LEN = 160
_RUNTIME_JUDGEHOST_HEALTH_CACHE_TTL_SEC = 2.0


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


def _runtime_judgehost_health_profile() -> dict[str, str]:
    global _RUNTIME_JUDGEHOST_HEALTH_CACHE, _RUNTIME_JUDGEHOST_HEALTH_CACHE_TS
    now = time.monotonic()
    if (
        isinstance(_RUNTIME_JUDGEHOST_HEALTH_CACHE, dict)
        and (now - float(_RUNTIME_JUDGEHOST_HEALTH_CACHE_TS)) <= _RUNTIME_JUDGEHOST_HEALTH_CACHE_TTL_SEC
    ):
        return dict(_RUNTIME_JUDGEHOST_HEALTH_CACHE)
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
        "runtime_judgehost_health_summary": _sanitize_runtime_profile_value(judgehost_summary),
        "runtime_judgehost_health_danger": "1" if judgehost_danger else "0",
        "runtime_judgehost_enabled": "1" if judgehost_enabled else "0",
        "runtime_judgehost_hosts_online": str(hosts_online),
        "runtime_judgehost_hosts_total": str(hosts_total),
    }
    _RUNTIME_JUDGEHOST_HEALTH_CACHE = dict(profile)
    _RUNTIME_JUDGEHOST_HEALTH_CACHE_TS = now
    return profile


def _startup_cancel_summary_rows(table_name: str, reason: str, *, now_text: str) -> None:
    warning_rows = config.runtime_state_service.cancel_inflight_summary_rows(table_name, reason, now_text=now_text)
    for message in warning_rows:
        warnings.warn(message, RuntimeWarning)


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
            service.cancel_all_domjudge_batches()
        except Exception as exc:
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)
    if not inflight_entries:
        return
    for item in inflight_entries:
        verification_id_raw = item.get("verification_id")
        verification_id = verification_id_raw.strip() if isinstance(verification_id_raw, str) else ""
        if not verification_id:
            continue
        verification_row_raw = config.verification_service.verification_record(verification_id)
        verification_row = dict(verification_row_raw) if verification_row_raw is not None else None
        if verification_row is None:
            continue
        status_raw = verification_row.get("status")
        status = status_raw.strip().lower() if isinstance(status_raw, str) else ""
        if status not in {"running", "queued", "pending"}:
            continue
        config.verification_service.cancel_unfinished_tasks(verification_id, reason=reason)
        try:
            config.verification_service.update_verification_record_status(
                verification_id=verification_id,
                status="failed",
                fail_reason=reason,
                finished=True,
            )
        except Exception as exc:
            warnings.warn(
                f"startup verification cancel failed for {verification_id}: {exc}",
                RuntimeWarning,
            )


def _startup_cancel_task_graph_verifications(reason: str) -> None:
    for verification_id in config.verification_service.verification_ids_with_unfinished_tasks():
        config.verification_service.cancel_unfinished_tasks(verification_id, reason=reason)
        try:
            config.verification_service.update_verification_record_status(
                verification_id,
                status="failed",
                fail_reason=reason,
                finished=True,
            )
        except Exception as exc:
            warnings.warn(f"startup task-graph verification reconciliation failed for {verification_id}: {exc}", RuntimeWarning)


def _startup_finalize_cancelled_verifications(now_text: str) -> None:
    try:
        config.verification_service.finalize_cancelled_unfinished_records(
            reason_predicate=is_cancel_reason,
            now_text=now_text,
        )
    except Exception as exc:
        warnings.warn(f"startup cancelled verification finalization failed: {exc}", RuntimeWarning)


def _startup_clear_all_caches() -> None:
    try:
        config.judge_fs_index_service.clear_all()
    except Exception as exc:
        warnings.warn(f"startup judge fs index clear failed: {exc}", RuntimeWarning)
    for root, label in (
        (config.fs_manager.cache_artifacts_root.resolve(), "artifact cache"),
        (config.fs_manager.runtime_root.resolve(), "runtime cache"),
    ):
        try:
            if root.exists() and root.is_dir() and (not root.is_symlink()):
                shutil.rmtree(root, ignore_errors=True)
        except Exception as exc:
            warnings.warn(f"startup {label} clear failed: {exc}", RuntimeWarning)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            warnings.warn(f"startup {label} recreate failed: {exc}", RuntimeWarning)
    durable_log_raw = str(_C.WORKER_QUEUE_DURABLE_LOG or "").strip()
    durable_log = (config.fs_manager.runtime_root / "worker-queue-events.jsonl").resolve()
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
    _startup_cancel_task_graph_verifications(cancel_reason)
    _startup_finalize_cancelled_verifications(now_text)
    _startup_clear_all_caches()


def startup() -> None:
    config.runtime_state_service.initialize_metadata()
    config.verification_service.apply_runtime_values(config.constants)
    _startup_reset_runtime_state()
    config.worker_queue_service.start()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)
