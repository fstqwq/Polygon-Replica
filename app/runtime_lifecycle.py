"""Startup and shutdown operations for an explicit application runtime."""

import warnings

from app.db import now_iso
from app.runtime import ApplicationRuntime
from app.service.platform.maintenance.filesystem import ArtifactCleanupFilesystem


def _startup_fail_summary_rows(
    runtime: ApplicationRuntime,
    table_name: str,
    reason: str,
    *,
    now_text: str,
) -> None:
    warning_rows = runtime.runtime_state_service.fail_inflight_summary_rows(
        table_name,
        reason,
        now_text=now_text,
    )
    for message in warning_rows:
        warnings.warn(message, RuntimeWarning)


def _startup_cancel_judgehost_inflight(
    runtime: ApplicationRuntime,
    reason: str,
) -> None:
    service = getattr(runtime, "judgehost_task_service", None)
    if service is not None:
        try:
            service.startup_cancel_inflight_tasks(reason=reason)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            warnings.warn(f"startup judgehost inflight scan failed: {exc}", RuntimeWarning)
    if service is not None:
        try:
            service.cancel_all_batches()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)


def _startup_clear_all_caches(runtime: ApplicationRuntime) -> None:
    runtime.worker_queue_service.reset_runtime_history()
    runtime.runtime_cache_index.clear_all()
    ArtifactCleanupFilesystem.clear_root(runtime.storage_layout.cache_root.resolve())


def _startup_reset_runtime_state(runtime: ApplicationRuntime) -> None:
    now_text = now_iso()
    failure_reason = "interrupted by application restart"
    _startup_fail_summary_rows(runtime, "previews", failure_reason, now_text=now_text)
    _startup_fail_summary_rows(
        runtime,
        "statement_previews",
        "statement preview cache cleared by application restart",
        now_text=now_text,
    )
    runtime.verification_service.recover_startup(reason=failure_reason)
    _startup_cancel_judgehost_inflight(runtime, failure_reason)
    _startup_clear_all_caches(runtime)


def startup(runtime: ApplicationRuntime) -> None:
    """Recover interrupted work and start the runtime worker queue once."""
    if runtime.schema_error is not None:
        return
    runtime.runtime_state_service.initialize_metadata()
    runtime.problem_package_service.recover_startup()
    runtime.export_service.fail_interrupted_export_jobs()
    _startup_reset_runtime_state(runtime)
    runtime.worker_queue_service.start()


def shutdown(runtime: ApplicationRuntime) -> None:
    """Stop process-owned workers for one application runtime."""
    if runtime.schema_error is not None:
        return
    try:
        runtime.worker_queue_service.stop()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        warnings.warn(f"shutdown worker queue stop failed: {exc}", RuntimeWarning)
