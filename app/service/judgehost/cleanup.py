from __future__ import annotations

import heapq
import threading
import time
from typing import Protocol

from app.service.judgehost.task_registry import JudgehostTaskRegistry


class _RuntimeCaseStore(Protocol):
    def forget_runs_if_quiet(self, run_ids: list[str]) -> int | None: ...

    def forget_scope(self, verification_id: str) -> None: ...


class _CaseBindingStore(Protocol):
    def unbind(
        self,
        verification_task_id: str,
        *,
        judgehost_task_id: str,
    ) -> bool: ...


class JudgehostTerminalCleanup:
    """One deadline scheduler for quiet terminal verifications.

    Runtime records must survive judgedaemon's asynchronous result and error
    retries, but scanning all historical records on fetch/status creates the
    scheduler DoS this cleanup replaces. Deadlines are verification-scoped;
    shared batches disappear only after their final owner's cases are forgotten.
    Content-addressed result and executable caches are deliberately untouched.
    """

    def __init__(
        self,
        task_registry: JudgehostTaskRegistry,
        case_store: _RuntimeCaseStore,
        case_binding_store: _CaseBindingStore,
        *,
        quiet_sec: float = 60.0,
    ) -> None:
        self._task_registry = task_registry
        self._case_store = case_store
        self._case_binding_store = case_binding_store
        self._quiet_sec = max(1.0, float(quiet_sec))
        self._condition = threading.Condition(threading.Lock())
        self._deadlines: list[tuple[float, int, str]] = []
        self._generation_by_verification: dict[str, int] = {}
        self._started = False

    def _ensure_started_locked(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(
            target=self._run,
            name="judgehost-terminal-cleanup",
            daemon=True,
        ).start()

    def schedule(self, verification_id: str) -> None:
        self._touch(verification_id, create=True)

    def touch(self, verification_id: str) -> None:
        self._touch(verification_id, create=False)

    def reset(self) -> None:
        """Discard scheduled terminal cleanup after an exclusive runtime reset."""

        with self._condition:
            self._deadlines.clear()
            self._generation_by_verification.clear()
            self._condition.notify_all()

    def _touch(self, verification_id: str, *, create: bool) -> None:
        if not verification_id:
            return
        with self._condition:
            if not create and verification_id not in self._generation_by_verification:
                return
            generation = self._generation_by_verification.get(verification_id, 0) + 1
            self._generation_by_verification[verification_id] = generation
            heapq.heappush(
                self._deadlines,
                (time.monotonic() + self._quiet_sec, generation, verification_id),
            )
            self._ensure_started_locked()
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._deadlines:
                    self._condition.wait()
                deadline, generation, verification_id = self._deadlines[0]
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                heapq.heappop(self._deadlines)
                if self._generation_by_verification.get(verification_id) != generation:
                    continue
            if not self._cleanup(verification_id, expected_generation=generation):
                self._touch(verification_id, create=True)

    def _cleanup(self, verification_id: str, *, expected_generation: int | None = None) -> bool:
        with self._condition:
            if (
                expected_generation is not None
                and self._generation_by_verification.get(verification_id) != expected_generation
            ):
                return True
            rows = self._task_registry.terminal_tasks_for_verification(verification_id)
            if rows is None:
                return False
            run_ids = [str(row["run_id"]) for row in rows]
            if self._case_store.forget_runs_if_quiet(run_ids) is None:
                return False
            self._case_store.forget_scope(verification_id)
            for row in rows:
                verification_task_id = str(
                    row.get("verification_task_id") or ""
                )
                if verification_task_id:
                    self._case_binding_store.unbind(
                        verification_task_id,
                        judgehost_task_id=str(row["id"]),
                    )
                self._task_registry.remove(str(row["id"]))
            self._generation_by_verification.pop(verification_id, None)
        return True
