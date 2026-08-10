from __future__ import annotations

import threading
from typing import Protocol

from app.service.verification.task_completion import CompletionCommit


class VerificationRuntimeAlreadyRegistered(RuntimeError):
    pass


class VerificationRuntimeHandle(Protocol):
    def enqueue_case_leased(self, verification_task_id: str) -> None: ...

    def enqueue_completion_committed(self, commit: CompletionCommit) -> None: ...

    def enqueue_completion_reconciliation(self, commit: CompletionCommit) -> None: ...

    def enqueue_cancel(self, reason: str) -> None: ...

    def enqueue_closed(self) -> None: ...


class VerificationRuntimeRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles_by_verification_id: dict[str, VerificationRuntimeHandle] = {}

    def register(
        self,
        verification_id: str,
        handle: VerificationRuntimeHandle,
    ) -> None:
        with self._lock:
            if verification_id in self._handles_by_verification_id:
                raise VerificationRuntimeAlreadyRegistered(
                    f"verification runtime is already registered: {verification_id}"
                )
            self._handles_by_verification_id[verification_id] = handle

    def unregister(
        self,
        verification_id: str,
        handle: VerificationRuntimeHandle,
    ) -> bool:
        with self._lock:
            current = self._handles_by_verification_id.get(verification_id)
            if current is not handle:
                return False
            self._handles_by_verification_id.pop(verification_id)
            return True

    def _handle(self, verification_id: str) -> VerificationRuntimeHandle | None:
        with self._lock:
            return self._handles_by_verification_id.get(verification_id)

    def case_leased(
        self,
        verification_id: str,
        verification_task_id: str,
    ) -> bool:
        handle = self._handle(verification_id)
        if handle is None:
            return False
        try:
            handle.enqueue_case_leased(verification_task_id)
        except Exception as delivery_error:
            current = self._handle(verification_id)
            if current is None:
                raise
            try:
                current.enqueue_case_leased(verification_task_id)
            except Exception as retry_error:
                raise RuntimeError(
                    "case-lease event delivery and retry failed: "
                    f"{delivery_error}; {retry_error}"
                ) from delivery_error
        return True

    def completion_committed(
        self,
        verification_id: str,
        commit: CompletionCommit,
    ) -> bool:
        handle = self._handle(verification_id)
        if handle is None:
            return False
        if commit.verification_id != verification_id:
            raise ValueError("completion commit verification does not match registry key")
        try:
            handle.enqueue_completion_committed(commit)
        except Exception as delivery_error:
            current = self._handle(verification_id)
            if current is None:
                raise
            try:
                current.enqueue_completion_reconciliation(commit)
            except Exception as reconciliation_error:
                raise RuntimeError(
                    "completion event delivery and durable reconciliation failed: "
                    f"{delivery_error}; {reconciliation_error}"
                ) from delivery_error
        return True

    def cancelled(self, verification_id: str, reason: str) -> bool:
        handle = self._handle(verification_id)
        if handle is None:
            return False
        handle.enqueue_cancel(reason)
        return True

    def closed(self, verification_id: str) -> bool:
        handle = self._handle(verification_id)
        if handle is None:
            return False
        handle.enqueue_closed()
        return True
