from __future__ import annotations

from pathlib import Path

from app.impl.runtime.config import config
from app.service.repository.revision import workspace_verification_source
from app.service.problem_package.service import PublishedRevision
from app.service.verification.types import Kind, Status
from app.service.verification.runtime import normalize_pass_limit, normalize_problem_mode

from app.impl.workspace.context_operation import audit
from app.service.verification.workspace_fingerprint import (
    remember_verification_fingerprint,
    verification_sources_fingerprint,
    verification_sources_signature,
)
from app.impl.workspace.published_materialization import ensure_published_materialization
from app.impl.workspace.problem_config import read_problem_config
from app.impl.workspace.verification_dag import run_workspace_verification_dag

_C = config.constants


def _workspace_mode_and_pass_limit(problem_id: int, workspace_id: int) -> tuple[str, int]:
    default_mode = str(_C.GENERAL_CONFIG_DEFAULTS.get("mode") or "pass-fail")
    default_pass_limit = int(_C.GENERAL_CONFIG_DEFAULTS.get("pass_limit") or 1)
    workspace_path_text = config.workspace_service.workspace_path(int(problem_id), int(workspace_id))
    if not workspace_path_text:
        return (default_mode, default_pass_limit)
    workspace_path = Path(workspace_path_text).resolve()
    _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
    return (
        normalize_problem_mode(general_cfg.get("mode"), default_mode),
        normalize_pass_limit(general_cfg.get("pass_limit"), default_pass_limit),
    )


def _verification_workspace_key(problem_id: int, workspace_id: int) -> str:
    return f"{int(problem_id)}:{int(workspace_id)}"

def _run_verification_start_worker(
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
    run_workspace_verification_dag(
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
    initial_details: dict[str, object] | None=None,
    initial_summary: dict[str, object] | None=None,
    workspace_path: Path | str | None=None,
    selected_test_names: list[str] | None=None,
    bypass_case_result_cache: bool = False,
    source_commit: str = "",
) -> bool:
    if initial_details is None and initial_summary is not None:
        initial_details = dict(initial_summary)
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
    if initial_details is not None:
        if signature and (not (initial_details.get("signature") or "")):
            initial_details["signature"] = signature
        initial_details.setdefault("verification_source", "verification.start")
    kind = _requested_verification_kind(selected_test_names=list(selected_test_names or []))
    record_source = str(source_commit or "").strip() or workspace_verification_source(workspace_head)
    with config.verification_lock:
        if key in config.verification_inflight:
            return False
        config.verification_inflight.add(key)
    if initial_details is not None:
        try:
            audit(actor_user_id, problem_id, 'verification.start', initial_details)
        except Exception:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            raise
    detail = {
        "mode": str(initial_details.get("mode") or "pass-fail") if initial_details is not None else "pass-fail",
        "pass_limit": int(initial_details.get("pass_limit") or 1) if initial_details is not None else 1,
        "source_paths": [str(item.get("path") or "") for item in targets if str(item.get("path") or "")],
        "selected_test_names": list(selected_test_names or []),
        "bypass_case_result_cache": bool(bypass_case_result_cache),
    }
    config.verification_service.begin_verification_record(
        verification_id=verification_id,
        problem_id=problem_id,
        workspace_id=workspace_id,
        signature=signature,
        source_commit=record_source,
        kind=kind,
        status=Status.RUNNING.value,
        detail=detail,
    )
    if kind == Kind.ALL.value and fingerprint and signature:
        remember_verification_fingerprint(
            problem_id,
            workspace_id,
            fingerprint,
            verification_id,
            signature,
        )
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            try:
                _run_verification_start_worker(
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
            except Exception as exc:
                detail = dict(config.verification_service.verification_detail(verification_id))
                detail["error"] = str(exc)
                config.verification_service.persist_verification_detail(verification_id, detail)
                config.verification_service.update_verification_record_status(
                    verification_id,
                    status=Status.FAILED.value,
                    fail_reason=str(exc),
                    finished=True,
                )
                raise
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.verification_lock:
                    config.verification_workers.discard(worker)
                    config.verification_inflight.discard(key)
    thread_name = verification_id if verification_id else key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            dedupe_key=f'verification:{key}',
            job_type='verification',
        )
        worker_ref[0] = worker
        if not queued:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'verification queue rejected ({submit_reason})')
        with config.verification_lock:
            config.verification_workers.add(worker)
    except Exception:
        with config.verification_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.verification_workers.discard(worker)
            config.verification_inflight.discard(key)
        raise
    return True

# Referenced via dynamic re-export in workspace.api/public.
_DYNAMIC_EXPORT_KEEP = (start_verification_job,)
_ = len(_DYNAMIC_EXPORT_KEEP)

def _export_key(problem_id: int, source_commit: str, export_type: str) -> str:
    return f"{int(problem_id)}:{source_commit}:{export_type}"


def _run_export_create_worker(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    requested_export_type: str,
    revision: PublishedRevision,
    export_job_id: str = "",
) -> None:
    if not export_job_id:
        raise ValueError("export_job_id is required")
    safe_export_type = requested_export_type or 'icpc'
    effective_source_commit = revision.source_commit
    try:
        config.export_service.mark_export_job_running(
            export_job_id,
            source_commit=effective_source_commit,
        )
        if safe_export_type not in {'icpc', 'native'}:
            raise ValueError('unsupported package type')
        if not effective_source_commit:
            raise ValueError('no committed revision; commit changes first')
        materialization = ensure_published_materialization(
            revision=revision,
            actor_user_id=actor_user_id,
            actor_username=user,
        )
        export_id, _out = config.export_service.create_export(
            problem,
            safe_export_type,
            materialization_id=materialization["id"],
        )
        config.export_service.mark_export_job_succeeded(
            export_job_id,
            materialization_id=materialization["id"],
            export_id=export_id,
        )
    except Exception as exc:
        config.export_service.mark_export_job_failed(export_job_id, str(exc))
        raise


def start_export_job(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    requested_export_type: str,
    export_job_id: str,
) -> bool:
    if not export_job_id:
        raise ValueError("export_job_id is required")
    revision = config.problem_package_service.published_revision(problem_id)
    source_commit = revision.source_commit
    key = f"{_export_key(problem_id, source_commit, requested_export_type)}:{export_job_id}"
    with config.export_lock:
        config.export_inflight.add(key)
    job_created = False
    try:
        config.export_service.create_export_job(
            job_id=export_job_id,
            problem_id=problem_id,
            actor_user_id=actor_user_id,
            export_type=requested_export_type,
            source_commit=source_commit,
        )
        job_created = True
        audit(
            actor_user_id,
            problem_id,
            'export.create',
            {
                'export_job_id': export_job_id,
                'export_type': requested_export_type,
                'source_commit': source_commit,
            },
        )
    except Exception as exc:
        if job_created:
            config.export_service.mark_export_job_failed(export_job_id, str(exc))
        with config.export_lock:
            config.export_inflight.discard(key)
        raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_export_create_worker(
                problem,
                user,
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                requested_export_type=requested_export_type,
                revision=revision,
                export_job_id=export_job_id,
            )
        finally:
            worker = worker_ref[0]
            with config.export_lock:
                config.export_inflight.discard(key)
                if worker is not None:
                    config.export_workers.discard(worker)
    thread_name = key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            dedupe_key=f'export-job:{export_job_id}',
            job_type='export',
        )
        worker_ref[0] = worker
        if not queued:
            with config.export_lock:
                config.export_inflight.discard(key)
            config.export_service.mark_export_job_failed(
                export_job_id,
                f'export queue rejected ({submit_reason})',
            )
            raise RuntimeError(f'export queue rejected ({submit_reason})')
        with config.export_lock:
            config.export_workers.add(worker)
            if not worker.is_alive():
                config.export_workers.discard(worker)
                config.export_inflight.discard(key)
    except Exception:
        if job_created:
            config.export_service.mark_export_job_failed(
                export_job_id,
                'export queue submission failed',
            )
        with config.export_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.export_workers.discard(worker)
            config.export_inflight.discard(key)
        raise
    return True
