from __future__ import annotations

import heapq
import threading
import time
from typing import Protocol

from .task_store import JudgehostTaskStore


class _RuntimeCaseStore(Protocol):
    def forget_runs(self, run_ids: list[str]) -> int: ...


class JudgehostTerminalCleanup:
    """One deadline scheduler for quiet terminal verifications.

    Runtime records must survive judgedaemon's asynchronous result and error
    retries, but scanning all historical records on fetch/status creates the
    scheduler DoS this cleanup replaces. Deadlines are verification-scoped;
    shared jobs disappear only after their final owner's cases are forgotten.
    Content-addressed result and executable caches are deliberately untouched.
    """

    def __init__(
        self,
        task_store: JudgehostTaskStore,
        state_store: _RuntimeCaseStore,
        *,
        quiet_sec: float = 60.0,
    ) -> None:
        self._task_store = task_store
        self._state_store = state_store
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
            rows = self._task_store.terminal_tasks_for_verification(verification_id)
            if rows is None:
                return False
            run_ids = [str(row["run_id"]) for row in rows]
            self._state_store.forget_runs(run_ids)
            for row in rows:
                self._task_store.remove(str(row["id"]))
            self._generation_by_verification.pop(verification_id, None)
        return True
