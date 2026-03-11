from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import IO

from app.service.judgehost.api import Judgehost
from app.service.run.api import Run

JUDGEHOST_BACKEND_NAME = "domjudge-judgehost"


@dataclass(frozen=True)
class InvocationBackendInfo:
    name: str
    ready: bool
    detail: str


class JudgehostDomserverInvocationBackend:
    # DOMserver role: queue task and wait for external judgehost completion.
    name = JUDGEHOST_BACKEND_NAME

    def __init__(self, run_service: Run, judgehost_task_service: Judgehost):
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
        task_kind: str = "",
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
            task_kind=str(task_kind or ""),
            force_recompile=bool(force_recompile),
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )
        return self._judgehost.wait_for_task(task_id, timeout_sec=None)

    def compile_only_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        invocation_source: str = "compile.only",
        expected_behavior: str = "compile",
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        if not self._judgehost.enabled():
            raise RuntimeError("judgehost backend is disabled")
        if not self._judgehost.auth_token_configured():
            raise RuntimeError("judgehost backend token is missing")
        safe_run_id = str(run_id or "").strip() or f"r-{uuid.uuid4().hex[:12]}"
        safe_invocation_id = str(invocation_id or "").strip() or f"inv-{uuid.uuid4().hex[:12]}"
        task_id = self._judgehost.enqueue_compile_only_task(
            problem=problem,
            username=username,
            build_id=build_id,
            upload_content=bytes(upload_content),
            upload_filename=str(upload_filename or "submission.cpp"),
            run_id=safe_run_id,
            invocation_id=safe_invocation_id,
            invocation_run_ids=list(invocation_run_ids or [safe_run_id]),
            expected_behavior=str(expected_behavior or "compile"),
            invocation_source=str(invocation_source or "compile.only"),
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
        run_service: Run,
        *,
        judgehost_task_service: Judgehost,
    ):
        self._judgehost_backend = JudgehostDomserverInvocationBackend(run_service, judgehost_task_service)

    def refresh(self) -> None:
        return None

    def apply_runtime_values(self, values: object) -> None:
        _ = values

    def active_backend_name(self) -> str:
        return JUDGEHOST_BACKEND_NAME

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
        task_kind: str = "",
        force_recompile: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self._judgehost_backend.run_submission(
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
            task_kind=task_kind,
            force_recompile=bool(force_recompile),
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )

    def compile_only_submission(
        self,
        *,
        problem: str,
        username: str,
        build_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str | None = None,
        invocation_id: str | None = None,
        invocation_run_ids: list[str] | None = None,
        invocation_source: str = "compile.only",
        expected_behavior: str = "compile",
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self._judgehost_backend.compile_only_submission(
            problem=problem,
            username=username,
            build_id=build_id,
            upload_content=bytes(upload_content),
            upload_filename=str(upload_filename or "submission.cpp"),
            run_id=run_id,
            invocation_id=invocation_id,
            invocation_run_ids=invocation_run_ids,
            invocation_source=invocation_source,
            expected_behavior=expected_behavior,
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )

    def status(self) -> dict[str, object]:
        judgehost_info = self._judgehost_backend.info()
        return {
            "configured": JUDGEHOST_BACKEND_NAME,
            "active": JUDGEHOST_BACKEND_NAME,
            "available": [
                {
                    "name": judgehost_info.name,
                    "ready": bool(judgehost_info.ready),
                    "detail": judgehost_info.detail,
                }
            ],
        }
