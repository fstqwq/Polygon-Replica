from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from typing import IO, Protocol

from app.services.run_service import RunService


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
        )

    def info(self) -> InvocationBackendInfo:
        return InvocationBackendInfo(name=self.name, ready=True, detail="built-in local sandbox")


class DomjudgeJudgehostInvocationBackend:
    name = "domjudge-judgehost"

    def __init__(self):
        adapter = str(os.getenv("POLYGONLIKE_DOMJUDGE_ADAPTER_CMD") or "").strip()
        self._adapter_cmd = adapter
        self._adapter_argv = shlex.split(adapter) if adapter else []
        timeout_raw = str(os.getenv("POLYGONLIKE_DOMJUDGE_ADAPTER_TIMEOUT_SEC") or "").strip()
        try:
            timeout_sec = int(timeout_raw)
        except Exception:
            timeout_sec = 30
        self._timeout_sec = max(5, min(300, timeout_sec))

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
    ) -> str:
        if not self._adapter_argv:
            raise RuntimeError(
                "domjudge backend is not configured: set POLYGONLIKE_DOMJUDGE_ADAPTER_CMD"
            )
        if upload_stream is not None:
            raise RuntimeError("domjudge adapter path does not support upload_stream")
        payload: dict[str, object] = {
            "problem": str(problem),
            "username": str(username),
            "build_id": str(build_id),
            "submission_path": submission_path,
            "mode": str(mode),
            "upload_filename": str(upload_filename or ""),
            "run_id": str(run_id or ""),
            "selected_tests": list(selected_tests or []),
            "invocation_id": str(invocation_id or ""),
            "invocation_run_ids": list(invocation_run_ids or []),
            "expected_behavior": str(expected_behavior or ""),
            "invocation_source": str(invocation_source or "run.execute"),
        }
        if isinstance(upload_content, (bytes, bytearray)):
            payload["upload_content_b64"] = base64.b64encode(bytes(upload_content)).decode("ascii")
        proc = subprocess.run(
            self._adapter_argv,
            input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=self._timeout_sec,
        )
        if int(proc.returncode or 0) != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            if len(stderr) > 400:
                stderr = stderr[:400].rstrip() + "..."
            detail = stderr or f"exit code {proc.returncode}"
            raise RuntimeError(f"domjudge adapter failed: {detail}")
        stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if not stdout_text:
            raise RuntimeError("domjudge adapter returned empty output")
        try:
            payload_out = json.loads(stdout_text)
        except Exception as exc:
            raise RuntimeError("domjudge adapter output must be JSON") from exc
        if not isinstance(payload_out, dict):
            raise RuntimeError("domjudge adapter output must be a JSON object")
        resolved_run_id = str(payload_out.get("run_id") or run_id or "").strip()
        if not resolved_run_id:
            raise RuntimeError("domjudge adapter output missing run_id")
        return resolved_run_id

    def info(self) -> InvocationBackendInfo:
        ready = bool(self._adapter_argv)
        if ready:
            detail = f"adapter command configured: {self._adapter_cmd}"
        else:
            detail = "set POLYGONLIKE_DOMJUDGE_ADAPTER_CMD"
        return InvocationBackendInfo(name=self.name, ready=ready, detail=detail)


class InvocationBackendService:
    def __init__(self, run_service: RunService):
        self._lock = threading.Lock()
        self._local_backend = LocalSandboxInvocationBackend(run_service)
        self._domjudge_backend = DomjudgeJudgehostInvocationBackend()
        self._backends: dict[str, InvocationBackend] = {
            self._local_backend.name: self._local_backend,
            self._domjudge_backend.name: self._domjudge_backend,
        }
        self._active_name = self._resolve_active_backend_name()

    def _resolve_active_backend_name(self) -> str:
        env_name = str(os.getenv("POLYGONLIKE_INVOCATION_BACKEND") or "").strip().lower()
        if env_name in self._backends:
            return env_name
        if env_name in {"domjudge", "judgehost"}:
            return "domjudge-judgehost"
        return "local-sandbox"

    def refresh_from_env(self) -> None:
        with self._lock:
            self._domjudge_backend = DomjudgeJudgehostInvocationBackend()
            self._backends[self._domjudge_backend.name] = self._domjudge_backend
            self._active_name = self._resolve_active_backend_name()

    def active_backend_name(self) -> str:
        with self._lock:
            return self._active_name

    def _active_backend(self) -> InvocationBackend:
        with self._lock:
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
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            active = self._active_name
            infos = [self._backends[name].info() for name in sorted(self._backends)]
        return {
            "active": active,
            "available": [
                {"name": item.name, "ready": bool(item.ready), "detail": item.detail}
                for item in infos
            ],
        }
