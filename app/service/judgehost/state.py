from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from typing import Callable

from app.db import DB
from app.runtime_value import RuntimeValues
from app.service.disk.verification_store import VerificationStore
from app.service.platform.fs.layout import FsManager
from app.service.platform.judge_fs_index import JudgeFsIndexService
from app.service.repository.workspace import WorkspaceService
from app.service.verification.task_store import VerificationTaskStore

from app.service.judgehost.task_registry import JudgehostTaskRegistry
from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.host_telemetry import HostTelemetryStore


@dataclass
class JudgehostState:
    db: DB
    workspace_service: WorkspaceService
    fs_manager: FsManager
    constants: RuntimeValues
    judge_fs_index_service: JudgeFsIndexService | None
    verification_task_store: VerificationTaskStore
    verification_store: VerificationStore = field(init=False)

    lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)

    enabled: bool = False
    api_token: str = ""
    api_username: str = "judgehost"
    fetch_batch_size: int = 2
    wait_timeout_sec: int = 900
    wait_poll_sec: float = 0.5
    online_window_sec: int = 120
    max_source_bytes: int = 262144
    max_tests_per_task: int = 512
    include_build_payload: bool = True
    max_binary_payload_bytes: int = 8388608

    task_registry: JudgehostTaskRegistry = field(init=False)
    touch_verification_runtime: Callable[[str], None] = field(
        init=False,
        default=lambda _verification_id: None,
    )
    hosts_state: dict[str, dict[str, object]] = field(default_factory=dict)
    peer_hostname_by_client_addr: dict[str, str] = field(default_factory=dict)
    host_telemetry: HostTelemetryStore = field(default_factory=HostTelemetryStore)
    batch_scheduler: BatchScheduler = field(default_factory=BatchScheduler)

    def __post_init__(self) -> None:
        self.verification_store = VerificationStore(self.db)
        self.task_registry = JudgehostTaskRegistry()
