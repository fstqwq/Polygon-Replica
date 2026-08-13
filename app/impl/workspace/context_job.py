from pathlib import Path

from app.runtime import ApplicationRuntime
from app.service.repository.revision import workspace_verification_source
from app.service.problem_package.service import PublishedRevision
from app.service.verification.lifecycle import VerificationAdmission
from app.service.verification.types import Kind

from app.service.verification.workspace_fingerprint import (
    remember_verification_fingerprint,
    verification_sources_fingerprint,
    verification_sources_signature,
)

def _verification_workspace_key(problem_id: int, workspace_id: int) -> str:
    return f"{int(problem_id)}:{int(workspace_id)}"

def _run_verification_start_worker(
    application_runtime: ApplicationRuntime,
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, object]],
    verification_id: str,
    signature: str='',
    source_commit: str = "",
    kind: str=Kind.ALL.value,
    selected_test_names: list[str] | None=None,
    bypass_case_result_cache: bool = False,
) -> None:
    application_runtime.verification_workflow.run(
        problem,
        user,
        actor_user_id=actor_user_id,
        problem_id=problem_id,
        workspace_id=workspace_id,
        workspace_head=workspace_head,
        workspace_dirty=workspace_dirty,
        targets=targets,
        verification_id=verification_id,
        signature=signature,
        source_commit=source_commit,
        kind=kind,
        selected_test_names=selected_test_names or [],
        bypass_case_result_cache=bypass_case_result_cache,
    )


def _requested_verification_kind(*, selected_test_names: list[str]) -> str:
    if selected_test_names:
        return Kind.CUSTOM.value
    return Kind.ALL.value
def start_verification_job(
    application_runtime: ApplicationRuntime,
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, object]],
    verification_id: str,
    workspace_path: Path | str | None=None,
    selected_test_names: list[str] | None=None,
    bypass_case_result_cache: bool = False,
    source_commit: str = "",
) -> bool:
    key = _verification_workspace_key(problem_id, workspace_id)
    fingerprint = ""
    signature = ""
    if workspace_path:
        try:
            workspace_obj = Path(workspace_path)
            fingerprint = verification_sources_fingerprint(workspace_obj)
            signature = verification_sources_signature(workspace_obj)
        except Exception:
            fingerprint = ""
            signature = ""
    kind = _requested_verification_kind(selected_test_names=list(selected_test_names or []))
    record_source = str(source_commit or "").strip() or workspace_verification_source(workspace_head)
    with application_runtime.verification_lock:
        if key in application_runtime.verification_inflight:
            return False
        application_runtime.verification_inflight.add(key)
    try:
        admission = application_runtime.verification_service.admit_verification(
            VerificationAdmission(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                signature=signature,
                source_commit=record_source,
                kind=kind,
            )
        )
    except Exception:
        with application_runtime.verification_lock:
            application_runtime.verification_inflight.discard(key)
        raise
    if admission.outcome != "admitted":
        with application_runtime.verification_lock:
            application_runtime.verification_inflight.discard(key)
        raise RuntimeError("verification id already exists")
    try:
        if kind == Kind.ALL.value and fingerprint and signature:
            remember_verification_fingerprint(
                problem_id,
                workspace_id,
                fingerprint,
                verification_id,
                signature,
            )
    except Exception as exc:
        application_runtime.verification_execution_service.fail_verification(
            verification_id,
            reason=str(exc) or "verification admission failed",
        )
        with application_runtime.verification_lock:
            application_runtime.verification_inflight.discard(key)
        raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_verification_start_worker(
                application_runtime,
                problem,
                user,
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
                targets=targets,
                verification_id=verification_id,
                signature=signature,
                source_commit=source_commit,
                kind=kind,
                selected_test_names=selected_test_names or [],
                bypass_case_result_cache=bypass_case_result_cache,
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with application_runtime.verification_lock:
                    application_runtime.verification_workers.discard(worker)
                    application_runtime.verification_inflight.discard(key)
    thread_name = verification_id if verification_id else key.replace(':', '-')
    try:
        worker, queued, submit_reason = application_runtime.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            dedupe_key=f'verification:{key}',
            job_type='verification',
        )
        worker_ref[0] = worker
        if not queued:
            with application_runtime.verification_lock:
                application_runtime.verification_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                application_runtime.verification_execution_service.fail_verification(
                    verification_id,
                    reason=f"verification queue rejected ({submit_reason})",
                )
                return False
            raise RuntimeError(f'verification queue rejected ({submit_reason})')
        with application_runtime.verification_lock:
            application_runtime.verification_workers.add(worker)
            if not worker.is_alive():
                application_runtime.verification_workers.discard(worker)
                application_runtime.verification_inflight.discard(key)
    except Exception as exc:
        application_runtime.verification_execution_service.fail_verification(
            verification_id,
            reason=str(exc) or "verification queue submission failed",
        )
        with application_runtime.verification_lock:
            worker = worker_ref[0]
            if worker is not None:
                application_runtime.verification_workers.discard(worker)
            application_runtime.verification_inflight.discard(key)
        raise
    return True

# Referenced via dynamic re-export in workspace.api/public.
_DYNAMIC_EXPORT_KEEP = (start_verification_job,)
_ = len(_DYNAMIC_EXPORT_KEEP)

def _export_key(problem_id: int, source_commit: str) -> str:
    return f"{int(problem_id)}:{source_commit}"


def _run_export_create_worker(
    application_runtime: ApplicationRuntime,
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    requested_format: str,
    revision: PublishedRevision,
    export_job_id: str = "",
) -> None:
    if not export_job_id:
        raise ValueError("export_job_id is required")
    package_format = requested_format
    effective_source_commit = revision.source_commit
    try:
        application_runtime.export_service.mark_export_job_running(
            export_job_id,
            source_commit=effective_source_commit,
        )
        if package_format not in {'domjudge', 'icpc-2025-09'}:
            raise ValueError('unsupported package format')
        if not effective_source_commit:
            raise ValueError('no committed revision; commit changes first')
        verified_revision = application_runtime.verified_revision_workflow.ensure(
            revision=revision,
            actor_user_id=actor_user_id,
            actor_username=user,
        )
        application_runtime.export_service.mark_export_job_projecting(
            export_job_id,
            verified_revision_id=verified_revision["id"],
        )
        export_id, _out, warning = application_runtime.export_service.create_export(
            problem,
            package_format,
            verified_revision_id=verified_revision["id"],
        )
        application_runtime.export_service.mark_export_job_succeeded(
            export_job_id,
            verified_revision_id=verified_revision["id"],
            export_id=export_id,
            warning=warning,
        )
    except Exception as exc:
        application_runtime.export_service.mark_export_job_failed(export_job_id, str(exc))
        raise


def start_export_job(
    application_runtime: ApplicationRuntime,
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    requested_format: str,
    export_job_id: str,
) -> bool:
    if not export_job_id:
        raise ValueError("export_job_id is required")
    revision = application_runtime.problem_package_service.published_revision(problem_id)
    source_commit = revision.source_commit
    key = _export_key(problem_id, source_commit)
    with application_runtime.export_lock:
        if key in application_runtime.export_inflight:
            return False
        application_runtime.export_inflight.add(key)
    job_created = False
    try:
        application_runtime.export_service.create_export_job(
            job_id=export_job_id,
            problem_id=problem_id,
            actor_user_id=actor_user_id,
            package_format=requested_format,
            source_commit=source_commit,
        )
        job_created = True
    except Exception as exc:
        if job_created:
            application_runtime.export_service.mark_export_job_failed(export_job_id, str(exc))
        with application_runtime.export_lock:
            application_runtime.export_inflight.discard(key)
        raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_export_create_worker(
                application_runtime,
                problem,
                user,
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                requested_format=requested_format,
                revision=revision,
                export_job_id=export_job_id,
            )
        finally:
            worker = worker_ref[0]
            with application_runtime.export_lock:
                application_runtime.export_inflight.discard(key)
                if worker is not None:
                    application_runtime.export_workers.discard(worker)
    thread_name = f"{key}:{export_job_id}".replace(':', '-')
    try:
        worker, queued, submit_reason = application_runtime.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            dedupe_key=f'export-job:{export_job_id}',
            job_type='export',
        )
        worker_ref[0] = worker
        if not queued:
            with application_runtime.export_lock:
                application_runtime.export_inflight.discard(key)
            application_runtime.export_service.mark_export_job_failed(
                export_job_id,
                f'export queue rejected ({submit_reason})',
            )
            raise RuntimeError(f'export queue rejected ({submit_reason})')
        with application_runtime.export_lock:
            application_runtime.export_workers.add(worker)
            if not worker.is_alive():
                application_runtime.export_workers.discard(worker)
                application_runtime.export_inflight.discard(key)
    except Exception:
        if job_created:
            application_runtime.export_service.mark_export_job_failed(
                export_job_id,
                'export queue submission failed',
            )
        with application_runtime.export_lock:
            worker = worker_ref[0]
            if worker is not None:
                application_runtime.export_workers.discard(worker)
            application_runtime.export_inflight.discard(key)
        raise
    return True
