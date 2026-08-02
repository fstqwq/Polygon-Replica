from __future__ import annotations

import heapq
import itertools
import threading
from collections import defaultdict
from datetime import datetime
from typing import TypedDict

from app.service.judgehost.runtime import parse_iso_utc
from app.service.platform.rwlock import WriterPriorityRWLock


class TaskLease(TypedDict):
    task_id: str
    run_id: str
    problem: str
    username: str
    artifact_verification_id: str
    mode: str
    lease_expires_at: str
    payload: dict[str, object]


class JudgehostTaskStore:
    """Indexed process-local task state.

    Heap entries carry a generation and are discarded lazily. This keeps enqueue,
    lease, and expiry operations logarithmic without periodic whole-store scans.
    """

    TERMINAL_STATUSES = frozenset({"completed", "failed"})
    ACTIVE_STATUSES = frozenset({"enqueuing", "queued", "dispatching", "leased", "reporting"})
    _ALLOWED_TRANSITIONS = {
        "enqueuing": frozenset({"queued", "failed"}),
        "queued": frozenset({"dispatching", "leased", "reporting", "failed"}),
        "dispatching": frozenset({"queued", "leased", "failed"}),
        "leased": frozenset({"queued", "reporting", "failed"}),
        "reporting": frozenset({"completed", "failed"}),
        "completed": frozenset(),
        "failed": frozenset(),
    }

    def __init__(self) -> None:
        self._lock = WriterPriorityRWLock()
        self._tasks: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._ready: list[tuple[int, int, str, int]] = []
        self._ready_by_group: dict[str, list[tuple[int, int, str, int]]] = defaultdict(list)
        self._lease_deadlines: list[tuple[float, str, int]] = []
        self._tasks_by_host: dict[str, set[str]] = defaultdict(set)
        self._tasks_by_problem: dict[str, set[str]] = defaultdict(set)
        self._tasks_by_verification: dict[str, set[str]] = defaultdict(set)
        self._tasks_by_group: dict[str, set[str]] = defaultdict(set)
        self._queued_counts_by_group: dict[str, int] = defaultdict(int)
        self._status_counts: dict[str, int] = defaultdict(int)
        self._sequence = itertools.count()
        self._changed = threading.Condition(threading.Lock())
        self._change_generation = 0

    @staticmethod
    def _copy(row: dict[str, object]) -> dict[str, object]:
        out = dict(row)
        payload = row.get("payload")
        if isinstance(payload, dict):
            out["payload"] = dict(payload)
        result = row.get("result")
        if isinstance(result, dict):
            out["result"] = dict(result)
        summary = row.get("summary")
        if isinstance(summary, dict):
            out["summary"] = dict(summary)
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

    def _generation(self, row: dict[str, object]) -> int:
        return int(row.get("runtime_generation") or 0)

    def _bump(self, row: dict[str, object]) -> int:
        generation = self._generation(row) + 1
        row["runtime_generation"] = generation
        return generation

    @staticmethod
    def _group_key(row: dict[str, object]) -> str:
        payload = row.get("payload")
        return str(payload.get("domjudge_group_key") or "") if isinstance(payload, dict) else ""

    @staticmethod
    def _priority_for_row(row: dict[str, object]) -> int:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return 10
        task_kind = str(payload.get("task_kind") or "").lower()
        source = str(payload.get("verification_source") or "").lower()
        if bool(payload.get("compile_only")) or task_kind == "compile-only" or source == "compile.only":
            return 0
        if task_kind == "generate-input" or source.endswith("generate-input"):
            return 1
        if task_kind == "main-correct" or source == "main-correct":
            return 2
        if source.startswith("sanity-check"):
            return 3
        return 10

    def _push_ready(self, row: dict[str, object]) -> None:
        entry = (
            self._priority_for_row(row),
            next(self._sequence),
            str(row["id"]),
            self._generation(row),
        )
        heapq.heappush(self._ready, entry)
        group_key = self._group_key(row)
        if group_key:
            heapq.heappush(self._ready_by_group[group_key], entry)

    def _set_status(self, row: dict[str, object], status: str) -> None:
        previous = str(row["status"])
        if previous == status:
            return
        if status not in self._ALLOWED_TRANSITIONS.get(previous, frozenset()):
            raise RuntimeError(f"invalid judgehost task transition: {previous} -> {status}")
        self._status_counts[previous] -= 1
        self._status_counts[status] += 1
        group_key = self._group_key(row)
        if group_key:
            if previous == "queued":
                self._queued_counts_by_group[group_key] -= 1
            if status == "queued":
                self._queued_counts_by_group[group_key] += 1
        row["status"] = status

    def insert(self, row: dict[str, object]) -> None:
        task_id = str(row["id"])
        run_id = str(row["run_id"])
        with self._lock.write_lock():
            if task_id in self._tasks or run_id in self._task_id_by_run:
                raise RuntimeError("judgehost task already exists")
            stored = self._copy(row)
            stored["runtime_generation"] = 1
            self._tasks[task_id] = stored
            self._task_id_by_run[run_id] = task_id
            self._tasks_by_problem[str(stored["problem_slug"])].add(task_id)
            verification_id = str(stored.get("verification_id") or "")
            if verification_id:
                self._tasks_by_verification[verification_id].add(task_id)
            group_key = self._group_key(stored)
            if group_key:
                self._tasks_by_group[group_key].add(task_id)
            self._status_counts[str(stored["status"])] += 1
            if stored["status"] == "queued":
                if group_key:
                    self._queued_counts_by_group[group_key] += 1
                self._push_ready(stored)
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

    def snapshots_for_host(self, hostname: str) -> list[dict[str, object]]:
        with self._lock.read_lock():
            return [
                self._copy(self._tasks[task_id])
                for task_id in self._tasks_by_host.get(hostname, ())
                if task_id in self._tasks
            ]

    def active_lease_counts(self, now_dt: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._lock.read_lock():
            for hostname, task_ids in self._tasks_by_host.items():
                active = 0
                for task_id in task_ids:
                    row = self._tasks.get(task_id)
                    if row is None or row["status"] != "leased":
                        continue
                    expires_at = parse_iso_utc(row.get("lease_expires_at"))
                    if expires_at is not None and expires_at >= now_dt:
                        active += 1
                if active:
                    counts[hostname] = active
        return counts

    def status_counts(self) -> dict[str, int]:
        with self._lock.read_lock():
            return {
                "queued": self._status_counts["queued"],
                "leased": self._status_counts["leased"],
                "completed": self._status_counts["completed"],
                "failed": self._status_counts["failed"],
            }

    def has_higher_priority_queued(self, priority: int) -> bool:
        with self._lock.write_lock():
            self._maybe_rebuild_ready(self._ready, group_key="")
            self._discard_stale_ready(self._ready)
            return bool(self._ready and self._ready[0][0] < priority)

    def _maybe_rebuild_ready(self, heap: list[tuple[int, int, str, int]], *, group_key: str) -> None:
        live_count = (
            self._queued_counts_by_group[group_key]
            if group_key
            else self._status_counts["queued"]
        )
        if len(heap) <= max(1024, live_count * 4):
            return
        task_ids = self._tasks_by_group.get(group_key, ()) if group_key else self._tasks.keys()
        heap[:] = [
            (
                self._priority_for_row(row),
                next(self._sequence),
                task_id,
                self._generation(row),
            )
            for task_id in task_ids
            if (row := self._tasks.get(task_id)) is not None and row["status"] == "queued"
        ]
        heapq.heapify(heap)

    def _discard_stale_ready(self, heap: list[tuple[int, int, str, int]]) -> None:
        while heap:
            _, _, task_id, generation = heap[0]
            row = self._tasks.get(task_id)
            if row is not None and row["status"] == "queued" and self._generation(row) == generation:
                return
            heapq.heappop(heap)

    def _claim_from_heap(
        self,
        heap: list[tuple[int, int, str, int]],
        *,
        hostname: str,
        lease_until: str,
        now_text: str,
    ) -> TaskLease | None:
        self._discard_stale_ready(heap)
        if not heap:
            return None
        _, _, task_id, generation = heapq.heappop(heap)
        row = self._tasks[task_id]
        if row["status"] != "queued" or self._generation(row) != generation:
            return None
        self._set_status(row, "leased")
        row["lease_owner"] = hostname
        row["lease_expires_at"] = lease_until
        row["updated_at"] = now_text
        row["attempt_count"] = int(row["attempt_count"]) + 1
        new_generation = self._bump(row)
        self._tasks_by_host[hostname].add(task_id)
        expires = parse_iso_utc(lease_until)
        if expires is not None:
            heapq.heappush(self._lease_deadlines, (expires.timestamp(), task_id, new_generation))
        return {
            "task_id": task_id,
            "run_id": str(row["run_id"]),
            "problem": str(row["problem_slug"]),
            "username": str(row["username"]),
            "artifact_verification_id": str(row["artifact_verification_id"]),
            "mode": str(row["mode"]),
            "lease_expires_at": lease_until,
            "payload": dict(row.get("payload") or {}),
        }

    def claim_ready(
        self,
        *,
        hostname: str,
        lease_until: str,
        now_text: str,
        group_key: str = "",
    ) -> TaskLease | None:
        with self._lock.write_lock():
            heap = self._ready_by_group[group_key] if group_key else self._ready
            self._maybe_rebuild_ready(heap, group_key=group_key)
            lease = self._claim_from_heap(
                heap,
                hostname=hostname,
                lease_until=lease_until,
                now_text=now_text,
            )
        if lease is not None:
            self._notify()
        return lease

    def renew(self, task_id: str, *, hostname: str | None, lease_until: str, now_text: str) -> bool:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None or row["status"] != "leased":
                return False
            owner = str(row["lease_owner"])
            if hostname is not None and owner != hostname:
                return False
            row["lease_expires_at"] = lease_until
            row["updated_at"] = now_text
            generation = self._bump(row)
            expires = parse_iso_utc(lease_until)
            if expires is not None:
                heapq.heappush(self._lease_deadlines, (expires.timestamp(), task_id, generation))
        self._notify()
        return True

    def expired_lease_candidates(self, now_dt: datetime, *, limit: int = 256) -> list[tuple[str, int]]:
        candidates: list[tuple[str, int]] = []
        now_ts = now_dt.timestamp()
        with self._lock.write_lock():
            while self._lease_deadlines and len(candidates) < limit:
                expires_at, task_id, generation = self._lease_deadlines[0]
                if expires_at > now_ts:
                    break
                heapq.heappop(self._lease_deadlines)
                row = self._tasks.get(task_id)
                if row is None or row["status"] != "leased" or self._generation(row) != generation:
                    continue
                candidates.append((task_id, generation))
        return candidates

    def requeue_if_generation(self, task_id: str, generation: int, *, now_text: str) -> bool:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None or row["status"] != "leased" or self._generation(row) != generation:
                return False
            owner = str(row["lease_owner"])
            if owner:
                self._tasks_by_host[owner].discard(task_id)
            self._set_status(row, "queued")
            row["lease_owner"] = ""
            row["lease_expires_at"] = ""
            row["updated_at"] = now_text
            self._bump(row)
            self._push_ready(row)
        self._notify()
        return True

    def requeue_host_tasks(self, hostname: str, *, excluded_task_ids: set[str], now_text: str) -> int:
        count = 0
        with self._lock.write_lock():
            for task_id in tuple(self._tasks_by_host.get(hostname, ())):
                row = self._tasks.get(task_id)
                if row is None or task_id in excluded_task_ids:
                    continue
                if row["status"] not in {"queued", "leased"}:
                    continue
                self._set_status(row, "queued")
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["updated_at"] = now_text
                self._bump(row)
                self._push_ready(row)
                self._tasks_by_host[hostname].discard(task_id)
                count += 1
        if count:
            self._notify()
        return count

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
            owner = str(row.get("lease_owner") or "")
            self._set_status(row, status)
            if updates:
                row.update(updates)
            self._bump(row)
            new_owner = str(row.get("lease_owner") or "")
            if owner and owner != new_owner:
                self._tasks_by_host[owner].discard(task_id)
            if new_owner:
                self._tasks_by_host[new_owner].add(task_id)
            if status == "queued":
                self._push_ready(row)
            snapshot = self._copy(row)
        self._notify()
        return snapshot

    def claim_reporting(self, task_id: str, *, lease_owner: str | None, now_text: str) -> dict[str, object] | None:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None:
                return None
            status = str(row["status"])
            if status in self.TERMINAL_STATUSES:
                return self._copy(row)
            if status not in {"queued", "leased"}:
                raise RuntimeError(f"judgehost task is not reportable (status={status})")
            if lease_owner is not None:
                owner = str(row.get("lease_owner") or "")
                if owner and owner != lease_owner:
                    raise RuntimeError("judgehost task lease owner mismatch")
            snapshot = self._copy(row)
            self._set_status(row, "reporting")
            row["updated_at"] = now_text
            self._bump(row)
        self._notify()
        return snapshot

    def restore_reporting(self, task_id: str, snapshot: dict[str, object], *, now_text: str) -> bool:
        with self._lock.write_lock():
            row = self._tasks.get(task_id)
            if row is None or row["status"] != "reporting":
                return False
            current_status = str(row["status"])
            restored = self._copy(snapshot)
            restored["runtime_generation"] = self._generation(row) + 1
            self._tasks[task_id] = restored
            self._status_counts[current_status] -= 1
            self._status_counts[str(restored["status"])] += 1
            restored["updated_at"] = now_text
            if restored["status"] == "queued":
                group_key = self._group_key(restored)
                if group_key:
                    self._queued_counts_by_group[group_key] += 1
                self._push_ready(restored)
            elif restored["status"] == "leased":
                expires = parse_iso_utc(restored.get("lease_expires_at"))
                if expires is not None:
                    heapq.heappush(
                        self._lease_deadlines,
                        (expires.timestamp(), task_id, self._generation(restored)),
                    )
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
            self._bump(row)
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
            owner = str(row.get("lease_owner") or "")
            if owner:
                self._tasks_by_host[owner].discard(task_id)
            self._tasks_by_problem[str(row["problem_slug"])].discard(task_id)
            verification_id = str(row.get("verification_id") or "")
            if verification_id:
                self._tasks_by_verification[verification_id].discard(task_id)
            group_key = self._group_key(row)
            if group_key:
                self._tasks_by_group[group_key].discard(task_id)
                if row["status"] == "queued":
                    self._queued_counts_by_group[group_key] -= 1
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
            self._ready.clear()
            self._ready_by_group.clear()
            self._lease_deadlines.clear()
            self._tasks_by_host.clear()
            self._tasks_by_problem.clear()
            self._tasks_by_verification.clear()
            self._tasks_by_group.clear()
            self._queued_counts_by_group.clear()
            self._status_counts.clear()
        self._notify()
