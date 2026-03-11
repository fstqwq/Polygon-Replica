from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


WorkerFunc = Callable[[], None]


@dataclass
class WorkerJobRecord:
    id: str
    name: str
    job_type: str
    queue_name: str
    backend: str
    dedupe_key: str
    status: str
    created_at: float
    started_at: float
    finished_at: float
    error: str
    error_code: str


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
    def __init__(
        self,
        *,
        worker_count: int | None = None,
        history_limit: int | None = None,
        queue_capacity: int | None = None,
        durable_log_path: Path | str | None = None,
        durable_history_limit: int | None = None,
    ) -> None:
        self._worker_count = (
            max(1, min(64, int(worker_count)))
            if worker_count is not None
            else 4
        )
        self._history_limit = (
            max(32, min(10000, int(history_limit)))
            if history_limit is not None
            else 1024
        )
        self._queue_capacity = (
            max(1, min(100000, int(queue_capacity)))
            if queue_capacity is not None
            else 512
        )
        self._durable_history_limit = (
            max(256, min(200000, int(durable_history_limit)))
            if durable_history_limit is not None
            else 20000
        )
        durable_raw = durable_log_path
        durable_text = str(durable_raw or "").strip()
        self._durable_log_path = Path(durable_text).resolve() if durable_text else None

        self._queue: queue.Queue[object] = queue.Queue(maxsize=self._queue_capacity)
        self._lock = threading.Lock()
        self._started = False
        self._workers: list[threading.Thread] = []
        self._records: dict[str, WorkerJobRecord] = {}
        self._record_order: list[str] = []
        self._futures: dict[str, WorkerFuture] = {}
        self._dedupe: dict[str, WorkerFuture] = {}
        self._sentinel = object()
        if self._durable_log_path is not None:
            try:
                self._durable_log_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._durable_log_path = None
        with self._lock:
            self._load_durable_history_locked()

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _normalize_job_type(self, raw: object) -> str:
        token = str(raw or "").strip().lower().replace("_", "-")
        if not token:
            return "generic"
        parts = [part for part in token.split("-") if part]
        safe = "-".join(parts)
        if not safe:
            return "generic"
        clipped = safe[:64]
        cleaned = "".join((ch for ch in clipped if ch.isalnum() or ch == "-"))
        return cleaned or "generic"

    def _sanitize_error_code(self, raw: object) -> str:
        token = str(raw or "").strip().lower().replace(" ", "_")
        if not token:
            return ""
        clipped = token[:64]
        cleaned = "".join((ch for ch in clipped if ch.isalnum() or ch in {"_", "-"}))
        return cleaned or ""

    def _queue_size_locked(self) -> int:
        try:
            return int(self._queue.qsize())
        except Exception:
            return 0

    def _classify_error(self, error: Exception | None) -> str:
        if error is None:
            return ""
        explicit = self._sanitize_error_code(getattr(error, "error_code", ""))
        if explicit:
            return explicit
        text = str(error or "").strip().lower()
        if not text:
            return "worker_error"
        if "queue rejected" in text:
            return "queue_rejected"
        if "cancel" in text:
            return "cancelled"
        if "timeout" in text:
            return "timeout"
        if "sandbox" in text or "bwrap" in text or "seccomp" in text:
            return "sandbox_error"
        if "compile" in text or "javac" in text or "g++" in text:
            return "compile_error"
        if "runtime" in text or "segmentation" in text:
            return "runtime_error"
        return "worker_error"

    def _append_durable_event_locked(self, event: dict[str, object]) -> None:
        if self._durable_log_path is None:
            return
        payload = dict(event)
        payload["ts"] = self._safe_float(payload.get("ts"), time.time())
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._durable_log_path.open("a", encoding="utf-8") as fp:
                fp.write(encoded + "\n")
        except OSError:
            return

    def _register_record_locked(self, record: WorkerJobRecord) -> None:
        if record.id in self._records:
            self._records[record.id] = record
            return
        self._records[record.id] = record
        self._record_order.append(record.id)

    def _load_durable_history_locked(self) -> None:
        if self._durable_log_path is None:
            return
        if not self._durable_log_path.exists():
            return
        lines: list[str]
        try:
            lines = self._durable_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if not lines:
            return
        if len(lines) > self._durable_history_limit:
            lines = lines[-self._durable_history_limit :]
        for line in lines:
            text = str(line or "").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            self._apply_durable_event_locked(event)
        self._recover_inflight_jobs_locked()
        self._prune_locked()

    def _record_from_event(self, event: dict[str, object], *, default_status: str = "queued") -> WorkerJobRecord:
        job_id = str(event.get("job_id") or "").strip()
        created = self._safe_float(event.get("created_at"), self._safe_float(event.get("ts"), time.time()))
        started = self._safe_float(event.get("started_at"), 0.0)
        finished = self._safe_float(event.get("finished_at"), 0.0)
        return WorkerJobRecord(
            id=job_id,
            name=str(event.get("name") or "job").strip() or "job",
            job_type=self._normalize_job_type(event.get("job_type")),
            queue_name=str(event.get("queue_name") or "default").strip() or "default",
            backend=str(event.get("backend") or "domjudge-judgehost").strip() or "domjudge-judgehost",
            dedupe_key=str(event.get("dedupe_key") or "").strip(),
            status=str(event.get("status") or default_status).strip().lower() or default_status,
            created_at=created,
            started_at=started,
            finished_at=finished,
            error=str(event.get("error") or "").strip(),
            error_code=self._sanitize_error_code(event.get("error_code")),
        )

    def _apply_durable_event_locked(self, event: dict[str, object]) -> None:
        event_type = str(event.get("event") or "").strip().lower()
        job_id = str(event.get("job_id") or "").strip()
        if not event_type or not job_id:
            return
        record = self._records.get(job_id)
        if record is None:
            record = self._record_from_event(event)
            self._register_record_locked(record)
        if event_type == "job_created":
            record.name = str(event.get("name") or record.name).strip() or record.name
            record.job_type = self._normalize_job_type(event.get("job_type") or record.job_type)
            record.queue_name = str(event.get("queue_name") or record.queue_name).strip() or record.queue_name
            record.backend = str(event.get("backend") or record.backend).strip() or record.backend
            record.dedupe_key = str(event.get("dedupe_key") or record.dedupe_key).strip()
            record.status = "queued"
            record.created_at = self._safe_float(event.get("created_at"), record.created_at or self._safe_float(event.get("ts"), time.time()))
            return
        if event_type == "job_started":
            record.status = "running"
            record.started_at = self._safe_float(event.get("started_at"), self._safe_float(event.get("ts"), time.time()))
            return
        if event_type in {"job_finished", "job_recovered"}:
            status = str(event.get("status") or "").strip().lower()
            if status not in {"done", "failed", "rejected", "cancelled"}:
                status = "failed"
            record.status = status
            record.started_at = self._safe_float(event.get("started_at"), record.started_at)
            record.finished_at = self._safe_float(event.get("finished_at"), self._safe_float(event.get("ts"), time.time()))
            record.error = str(event.get("error") or "").strip()
            record.error_code = self._sanitize_error_code(event.get("error_code"))
            return

    def _recover_inflight_jobs_locked(self) -> None:
        now_ts = time.time()
        for record in self._records.values():
            if record.status not in {"queued", "running"}:
                continue
            record.status = "cancelled"
            record.finished_at = now_ts
            record.error = "worker restarted: cancelled on startup"
            record.error_code = "worker_cancelled"
            self._append_durable_event_locked(
                {
                    "event": "job_recovered",
                    "job_id": record.id,
                    "status": record.status,
                    "error": record.error,
                    "error_code": record.error_code,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "ts": now_ts,
                }
            )

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
        job_type: str = "generic",
        queue_name: str = "default",
        backend: str = "domjudge-judgehost",
        dedupe_key: str = "",
    ) -> tuple[WorkerFuture, bool, str]:
        if not callable(fn):
            raise ValueError("worker job fn must be callable")
        if not self._started:
            self.start()
        safe_name = str(name or "job").strip() or "job"
        safe_job_type = self._normalize_job_type(job_type)
        safe_queue_name = str(queue_name or "default").strip() or "default"
        safe_backend = str(backend or "domjudge-judgehost").strip() or "domjudge-judgehost"
        safe_dedupe_key = str(dedupe_key or "").strip()
        with self._lock:
            if safe_dedupe_key:
                existing = self._dedupe.get(safe_dedupe_key)
                if existing is not None and existing.is_alive():
                    return (existing, False, "dedupe_inflight")
                if existing is not None and (not existing.is_alive()):
                    self._dedupe.pop(safe_dedupe_key, None)
            if self._queue_size_locked() >= self._queue_capacity:
                future = WorkerFuture(f"wq-{uuid.uuid4().hex[:12]}")
                err = RuntimeError("queue rejected: queue is full")
                future._mark_done(err)
                record = WorkerJobRecord(
                    id=future.job_id,
                    name=safe_name,
                    job_type=safe_job_type,
                    queue_name=safe_queue_name,
                    backend=safe_backend,
                    dedupe_key=safe_dedupe_key,
                    status="rejected",
                    created_at=time.time(),
                    started_at=0.0,
                    finished_at=time.time(),
                    error=str(err),
                    error_code="queue_rejected",
                )
                self._register_record_locked(record)
                self._futures[future.job_id] = future
                self._append_durable_event_locked(
                    {
                        "event": "job_created",
                        "job_id": record.id,
                        "name": record.name,
                        "job_type": record.job_type,
                        "queue_name": record.queue_name,
                        "backend": record.backend,
                        "dedupe_key": record.dedupe_key,
                        "created_at": record.created_at,
                        "ts": record.created_at,
                    }
                )
                self._append_durable_event_locked(
                    {
                        "event": "job_finished",
                        "job_id": record.id,
                        "status": record.status,
                        "error": record.error,
                        "error_code": record.error_code,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "ts": record.finished_at,
                    }
                )
                self._prune_locked()
                return (future, False, "queue_rejected_full")
            job_id = f"wq-{uuid.uuid4().hex[:12]}"
            future = WorkerFuture(job_id)
            record = WorkerJobRecord(
                id=job_id,
                name=safe_name,
                job_type=safe_job_type,
                queue_name=safe_queue_name,
                backend=safe_backend,
                dedupe_key=safe_dedupe_key,
                status="queued",
                created_at=time.time(),
                started_at=0.0,
                finished_at=0.0,
                error="",
                error_code="",
            )
            self._register_record_locked(record)
            self._futures[job_id] = future
            if safe_dedupe_key:
                self._dedupe[safe_dedupe_key] = future
            self._append_durable_event_locked(
                {
                    "event": "job_created",
                    "job_id": job_id,
                    "name": safe_name,
                    "job_type": safe_job_type,
                    "queue_name": safe_queue_name,
                    "backend": safe_backend,
                    "dedupe_key": safe_dedupe_key,
                    "created_at": record.created_at,
                    "ts": record.created_at,
                }
            )
            self._prune_locked()
            try:
                self._queue.put_nowait((job_id, safe_dedupe_key, fn))
            except queue.Full:
                err = RuntimeError("queue rejected: queue is full")
                record.status = "rejected"
                record.error = str(err)
                record.error_code = "queue_rejected"
                record.finished_at = time.time()
                future._mark_done(err)
                self._append_durable_event_locked(
                    {
                        "event": "job_finished",
                        "job_id": job_id,
                        "status": record.status,
                        "error": record.error,
                        "error_code": record.error_code,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "ts": record.finished_at,
                    }
                )
                self._prune_locked()
                return (future, False, "queue_rejected_full")
            return (future, True, "queued")

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
                if not self._mark_running(job_id):
                    continue
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

    def _mark_running(self, job_id: str) -> bool:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return False
            if record.status != "queued":
                return False
            record.status = "running"
            record.started_at = time.time()
            self._append_durable_event_locked(
                {
                    "event": "job_started",
                    "job_id": record.id,
                    "started_at": record.started_at,
                    "ts": record.started_at,
                }
            )
            return True

    def _mark_finished(self, job_id: str, error: Exception | None) -> None:
        with self._lock:
            record = self._records.get(job_id)
            future = self._futures.get(job_id)
            if record is not None:
                current_status = str(record.status or "").strip().lower()
                if current_status in {"done", "failed", "rejected", "cancelled"}:
                    return
                if error is None:
                    record.status = "done"
                    record.error = ""
                    record.error_code = ""
                else:
                    record.status = "failed"
                    record.error = str(error)
                    record.error_code = self._classify_error(error)
                record.finished_at = time.time()
            if future is not None:
                future._mark_done(error)
            if record is not None:
                self._append_durable_event_locked(
                    {
                        "event": "job_finished",
                        "job_id": record.id,
                        "status": record.status,
                        "error": record.error,
                        "error_code": record.error_code,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "ts": record.finished_at,
                    }
                )
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

    def _p95(self, values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted((float(v) for v in values if v >= 0.0))
        if not ordered:
            return 0.0
        idx = int(round((len(ordered) - 1) * 0.95))
        return ordered[max(0, min(len(ordered) - 1, idx))]

    def _job_type_stats_locked(self) -> dict[str, dict[str, object]]:
        rows: dict[str, dict[str, object]] = {}
        for job_id in self._record_order:
            record = self._records.get(job_id)
            if record is None:
                continue
            key = record.job_type or "generic"
            bucket = rows.get(key)
            if bucket is None:
                bucket = {
                    "total": 0,
                    "queued": 0,
                    "running": 0,
                    "done": 0,
                    "failed": 0,
                    "rejected": 0,
                    "cancelled": 0,
                    "avg_wait_ms": 0.0,
                    "p95_wait_ms": 0.0,
                    "avg_run_ms": 0.0,
                    "p95_run_ms": 0.0,
                    "failure_rate": 0.0,
                    "_wait_samples": [],
                    "_run_samples": [],
                    "_fail_codes": {},
                }
                rows[key] = bucket
            bucket["total"] = int(bucket["total"]) + 1
            status = str(record.status or "").strip().lower()
            if status in {"queued", "running", "done", "failed", "rejected", "cancelled"}:
                bucket[status] = int(bucket[status]) + 1
            if record.started_at > 0.0 and record.created_at > 0.0:
                wait_ms = max(0.0, (record.started_at - record.created_at) * 1000.0)
                wait_samples = bucket["_wait_samples"]
                if isinstance(wait_samples, list):
                    wait_samples.append(wait_ms)
            if record.finished_at > 0.0 and record.started_at > 0.0:
                run_ms = max(0.0, (record.finished_at - record.started_at) * 1000.0)
                run_samples = bucket["_run_samples"]
                if isinstance(run_samples, list):
                    run_samples.append(run_ms)
            if status in {"failed", "rejected", "cancelled"}:
                fail_codes = bucket["_fail_codes"]
                if isinstance(fail_codes, dict):
                    code = self._sanitize_error_code(record.error_code) or "unknown"
                    fail_codes[code] = int(fail_codes.get(code, 0)) + 1
        for key, bucket in rows.items():
            wait_samples = bucket.get("_wait_samples")
            run_samples = bucket.get("_run_samples")
            fail_codes = bucket.get("_fail_codes")
            if isinstance(wait_samples, list) and wait_samples:
                bucket["avg_wait_ms"] = round(sum(wait_samples) / len(wait_samples), 3)
                bucket["p95_wait_ms"] = round(self._p95(wait_samples), 3)
            else:
                bucket["avg_wait_ms"] = 0.0
                bucket["p95_wait_ms"] = 0.0
            if isinstance(run_samples, list) and run_samples:
                bucket["avg_run_ms"] = round(sum(run_samples) / len(run_samples), 3)
                bucket["p95_run_ms"] = round(self._p95(run_samples), 3)
            else:
                bucket["avg_run_ms"] = 0.0
                bucket["p95_run_ms"] = 0.0
            total = max(1, int(bucket.get("total") or 0))
            failures = int(bucket.get("failed") or 0) + int(bucket.get("rejected") or 0) + int(bucket.get("cancelled") or 0)
            bucket["failure_rate"] = round(failures / total, 4)
            if isinstance(fail_codes, dict) and fail_codes:
                ordered_codes = sorted(fail_codes.items(), key=lambda item: (-int(item[1]), str(item[0])))
                bucket["top_failure_codes"] = [{"code": code, "count": int(count)} for code, count in ordered_codes[:3]]
            else:
                bucket["top_failure_codes"] = []
            bucket.pop("_wait_samples", None)
            bucket.pop("_run_samples", None)
            bucket.pop("_fail_codes", None)
            rows[key] = bucket
        return rows

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
                        "job_type": record.job_type,
                        "queue": record.queue_name,
                        "backend": record.backend,
                        "dedupe_key": record.dedupe_key,
                        "status": record.status,
                        "created_at": record.created_at,
                        "started_at": record.started_at,
                        "finished_at": record.finished_at,
                        "error": record.error,
                        "error_code": record.error_code,
                    }
                )
            running = sum(1 for item in jobs if str(item.get("status") or "") == "running")
            queued = sum(1 for item in jobs if str(item.get("status") or "") == "queued")
            return {
                "worker_count": len(self._workers),
                "queue_capacity": self._queue_capacity,
                "queue_depth": self._queue_size_locked(),
                "running": running,
                "queued": queued,
                "history_limit": self._history_limit,
                "durable_log": str(self._durable_log_path) if self._durable_log_path is not None else "",
                "job_type_stats": self._job_type_stats_locked(),
                "jobs": jobs,
            }
