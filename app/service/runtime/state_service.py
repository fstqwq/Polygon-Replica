from __future__ import annotations

from app.db import DB
from app.service.disk.runtime_state_store import RuntimeStateStore


class RuntimeStateService:
    def __init__(self, db: DB, store: RuntimeStateStore):
        self._db = db
        self._store = store

    def initialize_metadata(self) -> None:
        self._db.init()

    def cancel_inflight_summary_rows(self, table_name: str, reason: str, *, now_text: str) -> list[str]:
        return self._store.cancel_inflight_summary_rows(table_name, reason, now_text=now_text)
