from __future__ import annotations

import shutil
import warnings

from app.db import now_iso
from app.impl.runtime.config import config
from app.service.verification.types import is_cancel_reason

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
    _startup_cancel_judgehost_inflight(cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("verifications", cancel_reason, now_text=now_text)
    _startup_cancel_task_graph_verifications(cancel_reason)
    _startup_finalize_cancelled_verifications(now_text)
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
