from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import IO, Protocol

from app.runtime_values import RuntimeValues
from app.services.judgehost_service import JudgehostTaskService
from app.services.run_service import RunService

AUTO_BACKEND_NAME = "auto"


@dataclass(frozen=True)
class InvocationBackendInfo:
    name: str
    ready: bool
    detail: str


class InvocationBackend(Protocol):
    name: str

    def run_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: IO[bytes] | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        raise NotImplementedError

    def info(self) -> InvocationBackendInfo:
        raise NotImplementedError


class LocalSandboxInvocationBackend:
    name = "local-sandbox"

    def __init__(self, run_service: RunService):
        self._run_service = run_service

    def run_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: IO[bytes] | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self._run_service.run_submission(
            problem,
            username,
            build_id,
            submission_path=submission_path,
            mode=mode,
            upload_content=upload_content,
            upload_filename=upload_filename,
            upload_stream=upload_stream,
            run_id=run_id,
            selected_tests=selected_tests,
            invocation_id=invocation_id,
            invocation_run_ids=invocation_run_ids,
            expected_behavior=expected_behavior,
            invocation_source=invocation_source,
            force_recompile=bool(force_recompile),
        )

    def info(self) -> InvocationBackendInfo:
        return InvocationBackendInfo(name=self.name, ready=True, detail="built-in local sandbox")


class JudgehostDomserverInvocationBackend:
    # DOMserver role: queue task and wait for external judgehost completion.
    name = "domjudge-judgehost"

    def __init__(self, run_service: RunService, judgehost_task_service: JudgehostTaskService):
        self._run_service = run_service
        self._judgehost = judgehost_task_service

    def ready(self) -> bool:
        return bool(self._judgehost.enabled() and self._judgehost.auth_token_configured())

    def run_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: IO[bytes] | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        if not self._judgehost.enabled():
            raise RuntimeError("judgehost backend is disabled")
        if not self._judgehost.auth_token_configured():
            raise RuntimeError("judgehost backend token is missing")
        if upload_stream is not None:
            raise RuntimeError("judgehost backend does not support upload_stream")
        safe_run_id = str(run_id or "").strip() or f"r-{uuid.uuid4().hex[:12]}"
        task_id = self._judgehost.enqueue_task(
            problem=problem,
            username=username,
            build_id=build_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            run_id=safe_run_id,
            selected_tests=selected_tests,
            invocation_id=str(invocation_id or ""),
            invocation_run_ids=list(invocation_run_ids or []),
            expected_behavior=str(expected_behavior or "unknown"),
            invocation_source=str(invocation_source or "run.execute"),
            force_recompile=bool(force_recompile),
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )
        return self._judgehost.wait_for_task(task_id, timeout_sec=None)

    def info(self) -> InvocationBackendInfo:
        status = self._judgehost.status()
        queue = status.get("queue") if isinstance(status, dict) else {}
        queue_text = ""
        if isinstance(queue, dict):
            queue_text = f"queue queued={int(queue.get('queued', 0))}, leased={int(queue.get('leased', 0))}"
        ready = self.ready()
        if not self._judgehost.enabled():
            detail = "judgehost service disabled"
        elif not self._judgehost.auth_token_configured():
            detail = "set JUDGEHOST_API_TOKEN in system config"
        else:
            detail = f"judgehost queue ready ({queue_text})".strip()
        return InvocationBackendInfo(name=self.name, ready=ready, detail=detail)


class InvocationBackendService:
    def __init__(
        self,
        run_service: RunService,
        *,
        judgehost_task_service: JudgehostTaskService,
        configured_backend_name: str = AUTO_BACKEND_NAME,
    ):
        self._lock = threading.Lock()
        self._local_backend = LocalSandboxInvocationBackend(run_service)
        self._judgehost_backend = JudgehostDomserverInvocationBackend(run_service, judgehost_task_service)
        self._backends: dict[str, InvocationBackend] = {
            self._local_backend.name: self._local_backend,
            self._judgehost_backend.name: self._judgehost_backend,
        }
        self._configured_name = self._normalize_configured_backend_name(configured_backend_name)
        self._active_name = self._resolve_effective_backend_name(self._configured_name)

    def _normalize_configured_backend_name(self, configured_name: str) -> str:
        token = str(configured_name or "").strip().lower()
        if token in self._backends:
            return token
        if token == AUTO_BACKEND_NAME:
            return AUTO_BACKEND_NAME
        return self._local_backend.name

    def _resolve_effective_backend_name(self, configured_name: str) -> str:
        token = self._normalize_configured_backend_name(configured_name)
        if token == AUTO_BACKEND_NAME:
            if self._judgehost_backend.ready():
                return self._judgehost_backend.name
            return self._local_backend.name
        if token in self._backends:
            return token
        return self._local_backend.name

    def refresh(self) -> None:
        with self._lock:
            self._active_name = self._resolve_effective_backend_name(self._configured_name)

    def set_configured_backend_name(self, name: str) -> None:
        with self._lock:
            self._configured_name = self._normalize_configured_backend_name(name)
            self._active_name = self._resolve_effective_backend_name(self._configured_name)

    def apply_runtime_values(self, values: RuntimeValues) -> None:
        self.set_configured_backend_name(str(values.INVOCATION_BACKEND))

    def active_backend_name(self) -> str:
        with self._lock:
            self._active_name = self._resolve_effective_backend_name(self._configured_name)
            return self._active_name

    def _active_backend(self) -> InvocationBackend:
        with self._lock:
            self._active_name = self._resolve_effective_backend_name(self._configured_name)
            backend = self._backends.get(self._active_name)
            if backend is None:
                backend = self._local_backend
                self._active_name = self._local_backend.name
            return backend

    def run_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        submission_path: str | None = None,
        mode: str = "pass-fail",
        upload_content: bytes | None = None,
        upload_filename: str | None = None,
        upload_stream: IO[bytes] | None = None,
        run_id: str | None = None,
        selected_tests: list[str] | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        expected_behavior: str | None = None,
        invocation_source: str = "run.execute",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        backend = self._active_backend()
        return backend.run_submission(
            problem=problem,
            username=username,
            build_id=build_id,
            submission_path=submission_path,
            mode=mode,
            upload_content=upload_content,
            upload_filename=upload_filename,
            upload_stream=upload_stream,
            run_id=run_id,
            selected_tests=selected_tests,
            invocation_id=invocation_id,
            invocation_run_ids=invocation_run_ids,
            expected_behavior=expected_behavior,
            invocation_source=invocation_source,
            force_recompile=bool(force_recompile),
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            configured = self._configured_name
            active = self._resolve_effective_backend_name(self._configured_name)
            self._active_name = active
            infos = [self._backends[name].info() for name in sorted(self._backends)]
        return {
            "configured": configured,
            "active": active,
            "available": [
                {"name": item.name, "ready": bool(item.ready), "detail": item.detail}
                for item in infos
            ],
        }
