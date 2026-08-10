from __future__ import annotations

import shutil
import warnings

from app.db import now_iso
from app.impl.runtime.config import config

_C = config.config_values

def _runtime_judgehost_health_profile() -> dict[str, str]:
    try:
        status = config.judgehost_task_service.public_status()
    except Exception:
        return {
            "runtime_judgehost_health_summary": "offline",
            "runtime_judgehost_health_tone": "danger",
            "runtime_judgehost_enabled": "0",
            "runtime_judgehost_hosts_online": "0",
            "runtime_judgehost_hosts_total": "0",
        }
    return {
        "runtime_judgehost_health_summary": status["summary"],
        "runtime_judgehost_health_tone": status["tone"],
        "runtime_judgehost_enabled": "1" if status["enabled"] else "0",
        "runtime_judgehost_hosts_online": str(status["hosts_online"]),
        "runtime_judgehost_hosts_total": str(status["hosts_total"]),
    }


def _startup_cancel_summary_rows(table_name: str, reason: str, *, now_text: str) -> None:
    warning_rows = config.runtime_state_service.cancel_inflight_summary_rows(table_name, reason, now_text=now_text)
    for message in warning_rows:
        warnings.warn(message, RuntimeWarning)


def _startup_cancel_judgehost_inflight(reason: str) -> None:
    service = getattr(config, "judgehost_task_service", None)
    if service is not None:
        try:
            service.startup_cancel_inflight_tasks(reason=reason)
        except Exception as exc:
            warnings.warn(f"startup judgehost inflight scan failed: {exc}", RuntimeWarning)
    if service is not None:
        try:
            service.cancel_all_domjudge_batches()
        except Exception as exc:
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)


def _startup_clear_all_caches() -> None:
    try:
        config.worker_queue_service.reset_runtime_history()
    except Exception as exc:
        warnings.warn(f"startup worker queue history clear failed: {exc}", RuntimeWarning)
    try:
        config.runtime_cache_index.clear_all()
    except Exception as exc:
        warnings.warn(f"startup runtime cache index clear failed: {exc}", RuntimeWarning)
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


def _startup_reset_runtime_state() -> None:
    now_text = now_iso()
    cancel_reason = "cancelled on service startup"
    _startup_cancel_summary_rows("previews", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("contest_jobs", cancel_reason, now_text=now_text)
    config.verification_service.recover_startup(reason=cancel_reason)
    _startup_cancel_judgehost_inflight(cancel_reason)
    _startup_clear_all_caches()


def startup() -> None:
    config.runtime_state_service.initialize_metadata()
    config.problem_package_service.fail_interrupted_builds()
    config.export_service.fail_interrupted_export_jobs()
    config.verification_service.refresh_config_state(config.config_values)
    _startup_reset_runtime_state()
    config.worker_queue_service.start()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)
