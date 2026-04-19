from __future__ import annotations

from pathlib import Path

from app.impl.runtime.config import config
from app.service.verification.types import Kind, Status
from app.service.verification.runtime import normalize_pass_limit, normalize_problem_mode

from .context_operation import audit
from .context_verification import (
    normalize_run_id_token,
    _verification_sources_signature,
)
from .problem_config import read_problem_config
from .verification_dag import run_workspace_verification_dag

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
    kind: str=Kind.ALL.value,
    selected_test_names: list[str] | None=None,
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
        kind=kind,
        selected_test_names=selected_test_names or [],
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
) -> bool:
    if initial_details is None and initial_summary is not None:
        initial_details = dict(initial_summary)
    key = _verification_workspace_key(problem_id, workspace_id)
    signature = ""
    if workspace_path:
        try:
            workspace_obj = Path(workspace_path)
            signature = _verification_sources_signature(workspace_obj)
        except Exception:
            signature = ""
    if initial_details is not None:
        if signature and (not (initial_details.get("signature") or "")):
            initial_details["signature"] = signature
        initial_details.setdefault("verification_source", "verification.start")
    kind = _requested_verification_kind(selected_test_names=list(selected_test_names or []))
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
    }
    config.verification_service.begin_verification_record(
        verification_id=verification_id,
        problem_id=problem_id,
        workspace_id=workspace_id,
        signature=signature,
        kind=kind,
        status=Status.RUNNING.value,
        detail=detail,
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
                    kind=kind,
                    selected_test_names=selected_test_names or [],
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

def _export_source_commit(export_type: str, source_commit: str) -> str:
    _ = export_type
    return source_commit


def _export_workspace_key(problem_id: int, workspace_id: int, source_commit: str, export_type: str) -> str:
    effective_source_commit = _export_source_commit(export_type, source_commit)
    return f"{int(problem_id)}:{int(workspace_id)}:{effective_source_commit}:{export_type}"

def _run_export_create_worker(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, source_commit: str, requested_verification_id: str, requested_export_type: str, export_task_id: str = "") -> None:
    safe_requested_verification_id = normalize_run_id_token(requested_verification_id)
    safe_export_type = requested_export_type or 'icpc'
    effective_source_commit = _export_source_commit(safe_export_type, source_commit)
    details: dict[str, object] = {'status': 'failed', 'artifact_verification_id': safe_requested_verification_id, 'export_type': safe_export_type, 'source_commit': effective_source_commit, 'filename': '', 'error': '', 'export_task_id': export_task_id}
    worker_error: Exception | None = None
    try:
        if safe_export_type not in {'icpc', 'native'}:
            raise ValueError('unsupported package type')
        if not effective_source_commit:
            raise ValueError('no committed revision; commit changes first')
        out = config.export_service.create_export(
            problem,
            "",
            safe_export_type,
            workspace_id=int(workspace_id),
            source_commit=effective_source_commit,
        )
        details['status'] = 'ok'
        details['filename'] = out.name
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        worker_error = exc
    audit(actor_user_id, problem_id, 'export.create', details)
    if worker_error is not None:
        raise worker_error

def start_export_job(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, source_commit: str, requested_verification_id: str, requested_export_type: str, export_task_id: str = "", initial_details: dict[str, object] | None=None) -> bool:
    key = _export_workspace_key(problem_id, workspace_id, source_commit, requested_export_type)
    with config.export_lock:
        if key in config.export_inflight:
            return False
        config.export_inflight.add(key)
    if initial_details is not None:
        try:
            audit(actor_user_id, problem_id, 'export.create', initial_details)
        except Exception:
            with config.export_lock:
                config.export_inflight.discard(key)
            raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_export_create_worker(problem, user, actor_user_id=actor_user_id, problem_id=problem_id, workspace_id=workspace_id, source_commit=source_commit, requested_verification_id=requested_verification_id, requested_export_type=requested_export_type, export_task_id=export_task_id)
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.export_lock:
                    config.export_workers.discard(worker)
                    config.export_inflight.discard(key)
    thread_name = key.replace(':', '-')
    try:
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            dedupe_key=f'export:{key}',
            job_type='export',
        )
        worker_ref[0] = worker
        if not queued:
            with config.export_lock:
                config.export_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'export queue rejected ({submit_reason})')
        with config.export_lock:
            config.export_workers.add(worker)
    except Exception:
        with config.export_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.export_workers.discard(worker)
            config.export_inflight.discard(key)
        raise
    return True
