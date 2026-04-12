from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field

from app.db import DB
from app.runtime_value import RuntimeValues
from app.service.disk.verification_store import VerificationStore
from app.service.memory.judgehost_state_store import JudgehostStateStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.repository.workspace import WorkspaceService


@dataclass
class JudgehostState:
    db: DB
    workspace_service: WorkspaceService
    fs_manager: FsManager
    constants: RuntimeValues
    judge_fs_index_service: JudgeFsIndexService | None
    verification_store: VerificationStore = field(init=False)

    lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    testcase_registry_lock: threading.RLock = field(default_factory=threading.RLock)
    lease_requeue_lock: threading.Lock = field(default_factory=threading.Lock)

    enabled: bool = False
    api_token: str = ""
    api_username: str = "judgehost"
    fetch_batch_size: int = 1
    lease_sec: int = 120
    wait_timeout_sec: int = 900
    wait_poll_sec: float = 0.5
    online_window_sec: int = 120
    max_source_bytes: int = 262144
    max_tests_per_task: int = 512
    include_build_payload: bool = True
    max_binary_payload_bytes: int = 8388608

    lease_requeue_next_ts: float = 0.0
    tasks_by_id: dict[str, dict[str, object]] = field(default_factory=dict)
    task_id_by_run: dict[str, str] = field(default_factory=dict)
    hosts_state: dict[str, dict[str, object]] = field(default_factory=dict)
    peer_hostname_by_client_addr: dict[str, str] = field(default_factory=dict)
    host_judged_case_events: dict[str, list[float]] = field(default_factory=dict)
    host_last_judging: dict[str, dict[str, str]] = field(default_factory=dict)
    testcase_registry_by_hash: dict[str, dict[str, object]] = field(default_factory=dict)

    judgehost_state_store: JudgehostStateStore = field(default_factory=JudgehostStateStore)

    def __post_init__(self) -> None:
        self.verification_store = VerificationStore(self.db)
