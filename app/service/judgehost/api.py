from __future__ import annotations

import sqlite3
import threading

from app.db import DB
from app.runtime_value import RuntimeValues
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.run.api import Run
from app.setting import Settings

from .internal.core import JudgehostCoreMixin
from .internal.domjudge_dispatch import JudgehostDomjudgeDispatchMixin
from .internal.domjudge_result import JudgehostDomjudgeResultsMixin
from .internal.domjudge_util import JudgehostDomjudgeUtilsMixin
from .internal.enqueue import JudgehostEnqueueMixin
from .internal.queue import JudgehostQueueMixin


class Judgehost(
    JudgehostCoreMixin,
    JudgehostEnqueueMixin,
    JudgehostQueueMixin,
    JudgehostDomjudgeUtilsMixin,
    JudgehostDomjudgeDispatchMixin,
    JudgehostDomjudgeResultsMixin,
):
    STATUS_QUEUED = "queued"
    STATUS_LEASED = "leased"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_REPORTING = "reporting"
    CASE_CACHE_KIND = JudgeFsIndexService.KIND_CASE
    SOLVE_OUTPUT_CACHE_KIND = JudgeFsIndexService.KIND_SOLVE_OUTPUT

    def __init__(
        self,
        db: DB,
        run_service: Run,
        settings: Settings,
        constants: RuntimeValues,
        judge_fs_index_service: JudgeFsIndexService | None = None,
    ) -> None:
        self.db = db
        self._run_service = run_service
        self._settings = settings
        self._constants = constants
        self._lock = threading.Lock()
        self._enabled = False
        self._api_token = ""
        self._api_username = "judgehost"
        self._fetch_batch_size = 1
        self._lease_sec = 120
        self._wait_timeout_sec = 900
        self._wait_poll_sec = 0.5
        self._online_window_sec = 120
        self._max_source_bytes = 262144
        self._max_tests_per_task = 512
        self._max_test_payload_bytes = 1048576
        self._include_build_payload = True
        self._max_binary_payload_bytes = 8388608
        self._lease_requeue_lock = threading.Lock()
        self._lease_requeue_next_ts = 0.0
        self._testcase_registry_lock = threading.RLock()
        self._testcase_registry_next_id = 1
        self._testcase_registry_by_hash: dict[str, dict[str, object]] = {}
        self._testcase_registry_by_id: dict[int, dict[str, object]] = {}
        self._state_lock = threading.RLock()
        self._tasks_by_id: dict[str, dict[str, object]] = {}
        self._task_id_by_run: dict[str, str] = {}
        self._hosts_state: dict[str, dict[str, object]] = {}
        self._host_judged_case_events: dict[str, list[float]] = {}
        self._host_last_judging: dict[str, dict[str, str]] = {}
        self._domdb_lock = threading.RLock()
        self._domdb = sqlite3.connect(":memory:", check_same_thread=False)
        self._domdb.row_factory = sqlite3.Row
        self._init_domdb_schema()
        self._judge_fs_index_service = judge_fs_index_service
        self.apply_runtime_values(constants)

