import threading
from collections import defaultdict

from app.service.platform.rwlock import WriterPriorityRWLock


class JudgehostTaskRegistry:
    """Process-local task identities and result receipts.

    Cases are the execution units. This registry deliberately has no ready queue,
    priority, host ownership, or lease deadline; those belong to BatchScheduler.
    """

    TERMINAL_STATUSES = frozenset({"completed", "failed"})
    ACTIVE_STATUSES = frozenset({"enqueuing", "queued", "leased", "reporting"})
    _ALLOWED_TRANSITIONS = {
        "enqueuing": frozenset({"queued", "failed"}),
        "queued": frozenset({"leased", "reporting", "failed"}),
        "leased": frozenset({"reporting", "failed"}),
        "reporting": frozenset({"completed", "failed"}),
        "completed": frozenset(),
        "failed": frozenset(),
    }

    def __init__(self) -> None:
        self._lock = WriterPriorityRWLock()
        self._tasks: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._tasks_by_problem: dict[str, set[str]] = defaultdict(set)
        self._tasks_by_verification: dict[str, set[str]] = defaultdict(set)
        self._status_counts: dict[str, int] = defaultdict(int)
        self._changed = threading.Condition(threading.Lock())
        self._change_generation = 0

    @staticmethod
    def _copy(row: dict[str, object]) -> dict[str, object]:
        out = dict(row)
        for key in ("payload", "result", "summary"):
            value = row.get(key)
            if isinstance(value, dict):
                out[key] = dict(value)
        return out

    def _notify(self) -> None:
        with self._changed:
            self._change_generation += 1
            self._changed.notify_all()

    def change_generation(self) -> int:
        with self._changed:
            return self._change_generation

    def wait_for_change(self, generation: int, timeout: float) -> int:
        with self._changed:
            if self._change_generation == generation:
                self._changed.wait(timeout=max(0.0, timeout))
            return self._change_generation

    def _set_status(self, row: dict[str, object], status: str) -> None:
        previous = str(row["status"])
        if previous == status:
            return
        if status not in self._ALLOWED_TRANSITIONS[previous]:
            raise RuntimeError(f"invalid judgehost task transition: {previous} -> {status}")
        self._status_counts[previous] -= 1
        self._status_counts[status] += 1
        row["status"] = status

    def insert(self, row: dict[str, object]) -> None:
        task_id = str(row["id"])
        run_id = str(row["run_id"])
        with self._lock.write_lock():
            if task_id in self._tasks or run_id in self._task_id_by_run:
                raise RuntimeError("judgehost task already exists")
            stored = self._copy(row)
            self._tasks[task_id] = stored
            self._task_id_by_run[run_id] = task_id
            self._tasks_by_problem[str(stored["problem_slug"])].add(task_id)
            verification_id = str(stored.get("verification_id") or "")
            if verification_id:
                self._tasks_by_verification[verification_id].add(task_id)
            self._status_counts[str(stored["status"])] += 1
        self._notify()

    def task_id_for_run(self, run_id: str) -> str | None:
        with self._lock.read_lock():
            return self._task_id_by_run.get(run_id)

    def get(self, task_id: str) -> dict[str, object] | None:
        with self._lock.read_lock():
            row = self._tasks.get(task_id)
            return None if row is None else self._copy(row)

    def get_for_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock.read_lock():
            task_id = self._task_id_by_run.get(run_id)
            row = None if task_id is None else self._tasks.get(task_id)
            return None if row is None else self._copy(row)

    def snapshots(self) -> list[dict[str, object]]:
        with self._lock.read_lock():
            return [self._copy(row) for row in self._tasks.values()]

    def status_counts(self) -> dict[str, int]:
        with self._lock.read_lock():
            return {
                "queued": self._status_counts["enqueuing"] + self._status_counts["queued"],
                "leased": self._status_counts["leased"] + self._status_counts["reporting"],
                "completed": self._status_counts["completed"],
                "failed": self._status_counts["failed"],
            }

    def maintenance_counts(self) -> dict[str, int]:
        """Return each active admission state without aggregation."""

        with self._lock.read_lock():
            return {
                "queued": self._status_counts["enqueuing"] + self._status_counts["queued"],
                "leased": self._status_counts["leased"],
                "reporting": self._status_counts["reporting"],
            }

    def transition(
        self,
        task_id: str,
        *,
        expected: set[str],
        status: str,
        updates: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None or str(row["status"]) not in expected:
                return None
            self._set_status(row, status)
            if updates:
                row.update(updates)
            snapshot = self._copy(row)
        self._notify()
        return snapshot

    def claim_reporting(self, task_id: str, *, now_text: str) -> dict[str, object] | None:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None:
                return None
            status = str(row["status"])
            if status in self.TERMINAL_STATUSES:
                return self._copy(row)
            if status not in {"queued", "leased"}:
                raise RuntimeError(f"judgehost task is not reportable (status={status})")
            snapshot = self._copy(row)
            self._set_status(row, "reporting")
            row["updated_at"] = now_text
        self._notify()
        return snapshot

    def restore_reporting(self, task_id: str, snapshot: dict[str, object], *, now_text: str) -> bool:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None or row["status"] != "reporting":
                return False
            self._status_counts["reporting"] -= 1
            restored = self._copy(snapshot)
            restored["updated_at"] = now_text
            self._tasks[task_id] = restored
            self._status_counts[str(restored["status"])] += 1
        self._notify()
        return True

    def update(self, task_id: str, updates: dict[str, object]) -> dict[str, object] | None:
        if "status" in updates:
            raise RuntimeError("task status must change through transition")
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None:
                return None
            row.update(updates)
            snapshot = self._copy(row)
        self._notify()
        return snapshot

    def remove(self, task_id: str) -> dict[str, object] | None:
        with self._lock.write_lock():
            row = self._tasks.pop(task_id, None)
            if row is None:
                return None
            run_id = str(row["run_id"])
            if self._task_id_by_run.get(run_id) == task_id:
                self._task_id_by_run.pop(run_id, None)
            problem_slug = str(row["problem_slug"])
            problem_tasks = self._tasks_by_problem[problem_slug]
            problem_tasks.discard(task_id)
            if not problem_tasks:
                self._tasks_by_problem.pop(problem_slug, None)
            verification_id = str(row.get("verification_id") or "")
            if verification_id:
                verification_tasks = self._tasks_by_verification[verification_id]
                verification_tasks.discard(task_id)
                if not verification_tasks:
                    self._tasks_by_verification.pop(verification_id, None)
            self._status_counts[str(row["status"])] -= 1
            snapshot = self._copy(row)
        self._notify()
        return snapshot

    def remove_problem(self, problem_slug: str) -> int:
        with self._lock.read_lock():
            task_ids = tuple(self._tasks_by_problem.get(problem_slug, ()))
        return sum(1 for task_id in task_ids if self.remove(task_id) is not None)

    def terminal_tasks_for_verification(self, verification_id: str) -> list[dict[str, object]] | None:
        with self._lock.read_lock():
            task_ids = tuple(self._tasks_by_verification.get(verification_id, ()))
            rows = [self._tasks[task_id] for task_id in task_ids if task_id in self._tasks]
            if any(str(row["status"]) not in self.TERMINAL_STATUSES for row in rows):
                return None
            return [self._copy(row) for row in rows]

    def reset(self) -> None:
        with self._lock.write_lock():
            self._tasks.clear()
            self._task_id_by_run.clear()
            self._tasks_by_problem.clear()
            self._tasks_by_verification.clear()
            self._status_counts.clear()
        self._notify()
