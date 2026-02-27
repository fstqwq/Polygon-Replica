from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable


WorkerFunc = Callable[[], None]


@dataclass
class WorkerJobRecord:
    id: str
    name: str
    queue_name: str
    backend: str
    dedupe_key: str
    status: str
    created_at: float
    started_at: float
    finished_at: float
    error: str


class WorkerFuture:
    def __init__(self, job_id: str):
        self.job_id = str(job_id or "").strip()
        self._done_event = threading.Event()
        self._lock = threading.Lock()
        self._error: Exception | None = None

    def is_alive(self) -> bool:
        return not self._done_event.is_set()

    def join(self, timeout: float | None = None) -> None:
        wait_timeout = None if timeout is None else max(0.0, float(timeout))
        self._done_event.wait(wait_timeout)

    def exception(self) -> Exception | None:
        with self._lock:
            return self._error

    def _mark_done(self, error: Exception | None = None) -> None:
        with self._lock:
            self._error = error
        self._done_event.set()


class WorkerQueueService:
    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._lock = threading.Lock()
        self._started = False
        self._workers: list[threading.Thread] = []
        self._records: dict[str, WorkerJobRecord] = {}
        self._record_order: list[str] = []
        self._futures: dict[str, WorkerFuture] = {}
        self._dedupe: dict[str, WorkerFuture] = {}
        self._sentinel = object()
        self._worker_count = self._env_int("POLYGONLIKE_WORKER_THREADS", default=4, min_value=1, max_value=64)
        self._history_limit = self._env_int("POLYGONLIKE_WORKER_HISTORY_LIMIT", default=1024, min_value=32, max_value=10000)

    def _env_int(self, key: str, *, default: int, min_value: int, max_value: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except Exception:
            return default
        return max(min_value, min(max_value, value))

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._workers = []
            for idx in range(self._worker_count):
                worker = threading.Thread(target=self._worker_loop, daemon=True, name=f"worker-queue-{idx + 1}")
                self._workers.append(worker)
                worker.start()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            workers = list(self._workers)
        for _ in workers:
            self._queue.put(self._sentinel)
        for worker in workers:
            try:
                worker.join(timeout=1.0)
            except Exception:
                pass
        with self._lock:
            self._started = False
            self._workers = []

    def submit(
        self,
        *,
        name: str,
        fn: WorkerFunc,
        queue_name: str = "default",
        backend: str = "local-sandbox",
        dedupe_key: str = "",
    ) -> tuple[WorkerFuture, bool]:
        if not callable(fn):
            raise ValueError("worker job fn must be callable")
        if not self._started:
            self.start()
        safe_name = str(name or "job").strip() or "job"
        safe_queue_name = str(queue_name or "default").strip() or "default"
        safe_backend = str(backend or "local-sandbox").strip() or "local-sandbox"
        safe_dedupe_key = str(dedupe_key or "").strip()
        with self._lock:
            if safe_dedupe_key:
                existing = self._dedupe.get(safe_dedupe_key)
                if existing is not None and existing.is_alive():
                    return (existing, False)
                if existing is not None and (not existing.is_alive()):
                    self._dedupe.pop(safe_dedupe_key, None)
            job_id = f"wq-{uuid.uuid4().hex[:12]}"
            future = WorkerFuture(job_id)
            record = WorkerJobRecord(
                id=job_id,
                name=safe_name,
                queue_name=safe_queue_name,
                backend=safe_backend,
                dedupe_key=safe_dedupe_key,
                status="queued",
                created_at=time.time(),
                started_at=0.0,
                finished_at=0.0,
                error="",
            )
            self._records[job_id] = record
            self._record_order.append(job_id)
            self._futures[job_id] = future
            if safe_dedupe_key:
                self._dedupe[safe_dedupe_key] = future
            self._prune_locked()
            self._queue.put((job_id, safe_dedupe_key, fn))
            return (future, True)

    def _prune_locked(self) -> None:
        while len(self._record_order) > self._history_limit:
            victim_id = self._record_order.pop(0)
            victim = self._records.get(victim_id)
            if victim is None:
                continue
            future = self._futures.get(victim_id)
            if future is not None and future.is_alive():
                self._record_order.append(victim_id)
                break
            self._records.pop(victim_id, None)
            self._futures.pop(victim_id, None)

    def _worker_loop(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is self._sentinel:
                self._queue.task_done()
                return
            job_id = ""
            dedupe_key = ""
            fn: WorkerFunc | None = None
            try:
                if isinstance(payload, tuple) and len(payload) == 3:
                    job_id = str(payload[0] or "").strip()
                    dedupe_key = str(payload[1] or "").strip()
                    fn = payload[2] if callable(payload[2]) else None
                if not job_id:
                    continue
                self._mark_running(job_id)
                err: Exception | None = None
                if fn is None:
                    err = RuntimeError("worker job has no callable")
                else:
                    try:
                        fn()
                    except Exception as exc:
                        err = exc
                self._mark_finished(job_id, err)
                if dedupe_key:
                    with self._lock:
                        current = self._dedupe.get(dedupe_key)
                        future = self._futures.get(job_id)
                        if (current is not None) and (future is not None) and (current is future):
                            self._dedupe.pop(dedupe_key, None)
            finally:
                self._queue.task_done()

    def _mark_running(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            record.status = "running"
            record.started_at = time.time()

    def _mark_finished(self, job_id: str, error: Exception | None) -> None:
        with self._lock:
            record = self._records.get(job_id)
            future = self._futures.get(job_id)
            if record is not None:
                if error is None:
                    record.status = "done"
                    record.error = ""
                else:
                    record.status = "failed"
                    record.error = str(error)
                record.finished_at = time.time()
            if future is not None:
                future._mark_done(error)
            self._prune_locked()

    def wait_for_futures(self, futures: list[WorkerFuture], timeout_sec: float = 300.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        for future in futures:
            if not isinstance(future, WorkerFuture):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            future.join(remaining)

    def snapshot(self, limit: int = 200) -> dict[str, object]:
        cap = max(1, min(2000, int(limit)))
        with self._lock:
            job_ids = list(reversed(self._record_order[-cap:]))
            jobs: list[dict[str, object]] = []
            for job_id in job_ids:
                record = self._records.get(job_id)
                if record is None:
                    continue
                jobs.append(
                    {
                        "id": record.id,
                        "name": record.name,
                        "queue": record.queue_name,
                        "backend": record.backend,
                        "dedupe_key": record.dedupe_key,
                        "status": record.status,
                        "created_at": record.created_at,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "error": record.error,
                    }
                )
            running = sum(1 for item in jobs if str(item.get("status") or "") == "running")
            queued = sum(1 for item in jobs if str(item.get("status") or "") == "queued")
            return {
                "worker_count": len(self._workers),
                "queue_depth": self._queue.qsize(),
                "running": running,
                "queued": queued,
                "jobs": jobs,
            }
