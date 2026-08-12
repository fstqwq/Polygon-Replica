from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from typing import Callable

from app.config import ConfigValues
from app.service.judgehost.execution_port import JudgehostExecutionPort
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.repository.workspace import WorkspaceService

from app.service.judgehost.batch_scheduler import BatchScheduler
from app.service.judgehost.task_registry import JudgehostTaskRegistry
from app.service.judgehost.toolchain_versions import HostToolchainTelemetry


@dataclass(frozen=True)
class JudgehostPolicy:
    enabled: bool
    api_token: str
    api_username: str
    fetch_batch_size: int
    wait_timeout_sec: int
    wait_poll_sec: float
    online_window_sec: int
    max_submission_source_bytes: int
    max_tests_per_task: int
    max_component_source_bytes: int


@dataclass
class JudgehostState:
    workspace_service: WorkspaceService
    config_values: ConfigValues
    runtime_blob_store: RuntimeBlobStore
    runtime_cache_index: RuntimeCacheIndex
    execution_port: JudgehostExecutionPort
    lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)

    fetch_long_poll_sec: float = 5.0

    task_registry: JudgehostTaskRegistry = field(init=False)
    touch_verification_runtime: Callable[[str], None] = field(
        init=False,
        default=lambda _verification_id: None,
    )
    hosts_state: dict[str, dict[str, object]] = field(default_factory=dict)
    host_toolchains: dict[str, dict[str, HostToolchainTelemetry]] = field(default_factory=dict)
    batch_scheduler: BatchScheduler = field(default_factory=BatchScheduler)

    def __post_init__(self) -> None:
        self.task_registry = JudgehostTaskRegistry()

    def config_policy(self) -> JudgehostPolicy:
        snapshot = self.config_values.snapshot()
        return JudgehostPolicy(
            enabled=bool(snapshot["JUDGEHOST_ENABLE"]),
            api_token=str(snapshot["JUDGEHOST_API_TOKEN"]),
            api_username=str(snapshot["JUDGEHOST_API_USERNAME"]),
            fetch_batch_size=int(snapshot["JUDGEHOST_FETCH_BATCH_SIZE"]),
            wait_timeout_sec=int(snapshot["JUDGEHOST_WAIT_TIMEOUT_SEC"]),
            wait_poll_sec=float(snapshot["JUDGEHOST_WAIT_POLL_SEC"]),
            online_window_sec=int(snapshot["JUDGEHOST_ONLINE_WINDOW_SEC"]),
            max_submission_source_bytes=int(
                snapshot["JUDGEHOST_MAX_SUBMISSION_SOURCE_BYTES"]
            ),
            max_tests_per_task=int(snapshot["JUDGEHOST_MAX_TESTS_PER_TASK"]),
            max_component_source_bytes=int(
                snapshot["JUDGEHOST_MAX_COMPONENT_SOURCE_BYTES"]
            ),
        )
