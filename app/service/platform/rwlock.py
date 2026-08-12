import contextlib
import threading
from collections.abc import Iterator


class WriterPriorityRWLock:
    """Non-reentrant RWLock that stops admitting readers once a writer waits."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._reader_threads: dict[int, int] = {}
        self._writer_thread: int | None = None
        self._waiting_writers = 0

    def _ensure_not_held(self) -> int:
        thread_id = threading.get_ident()
        if self._writer_thread == thread_id or self._reader_threads.get(thread_id, 0):
            raise RuntimeError("RWLock is non-reentrant")
        return thread_id

    @contextlib.contextmanager
    def read_lock(self) -> Iterator[None]:
        with self._condition:
            thread_id = self._ensure_not_held()
            while self._writer_thread is not None or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
            self._reader_threads[thread_id] = 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                self._reader_threads.pop(thread_id, None)
                if self._readers == 0:
                    self._condition.notify_all()

    @contextlib.contextmanager
    def write_lock(self) -> Iterator[None]:
        with self._condition:
            thread_id = self._ensure_not_held()
            self._waiting_writers += 1
            try:
                while self._writer_thread is not None or self._readers:
                    self._condition.wait()
                self._writer_thread = thread_id
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer_thread = None
                self._condition.notify_all()
