from __future__ import annotations

import shutil
import warnings

from app.db import now_iso
from app.impl.runtime.config import config


def _startup_fail_summary_rows(table_name: str, reason: str, *, now_text: str) -> None:
    warning_rows = config.runtime_state_service.fail_inflight_summary_rows(
        table_name,
        reason,
        now_text=now_text,
    )
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
    failure_reason = "interrupted by application restart"
    _startup_fail_summary_rows("previews", failure_reason, now_text=now_text)
    _startup_fail_summary_rows("contest_jobs", failure_reason, now_text=now_text)
    config.verification_service.recover_startup(reason=failure_reason)
    _startup_cancel_judgehost_inflight(failure_reason)
    _startup_clear_all_caches()


def startup() -> None:
    if config.schema_error is not None:
        return
    config.runtime_state_service.initialize_metadata()
    config.problem_package_service.fail_interrupted_builds()
    config.export_service.fail_interrupted_export_jobs()
    _startup_reset_runtime_state()
    config.worker_queue_service.start()


def shutdown() -> None:
    if config.schema_error is not None:
        return
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)
