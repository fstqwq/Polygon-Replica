import time
from typing import TypedDict

from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.configuration import JudgehostConfiguration
from app.service.judgehost.ports.completion import CaseTerminalReport
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.result_view import project_task_case_result
from app.service.judgehost.task.summary import load_run_summary


class TaskPollResult(TypedDict):
    task_id: str
    verification_id: str
    run_id: str
    artifact_path: str
    status: str
    task_status: str
    error: str
    summary: dict[str, object]


class JudgehostTaskQuery:
    """Read task and case outcomes without changing their lifecycle."""

    def __init__(
        self,
        tasks: JudgehostTaskRegistry,
        batch_runtime: JudgehostBatchRuntime,
        configuration: JudgehostConfiguration,
    ) -> None:
        self._tasks = tasks
        self._batch_runtime = batch_runtime
        self._configuration = configuration

    def load_run_summary(self, run_id: str, verification_id: str = "") -> dict[str, object]:
        return load_run_summary(self._tasks, run_id, verification_id)

    def wait_for_task_result(
        self,
        task_id: str,
        timeout_sec: float | None = None,
    ) -> TaskPollResult:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        configured = self._configuration.snapshot().wait_timeout_sec
        timeout = configured if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        generation = self._tasks.change_generation()
        while True:
            result = self.poll_task_result(task_id)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            generation = self._tasks.wait_for_change(generation, remaining)

    def poll_task_result(self, task_id: str) -> TaskPollResult | None:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        row = self._tasks.get(task_id)
        if row is None:
            raise RuntimeError("judgehost task disappeared")
        if row["status"] not in {"completed", "failed"}:
            return None
        return {
            "task_id": task_id,
            "verification_id": row["verification_id"],
            "run_id": row["run_id"],
            "artifact_path": "",
            "status": row["run_status"],
            "task_status": row["status"],
            "error": row["error_text"],
            "summary": row["summary"].copy(),
        }

    def wait_for_task_case_result(
        self,
        task_id: str,
        test_name: str,
        timeout_sec: float | None = None,
    ) -> CaseTerminalReport:
        if not task_id:
            raise RuntimeError("judgehost task id is required")
        if not test_name:
            raise RuntimeError("judgehost test name is required")
        configured = self._configuration.snapshot().wait_timeout_sec
        timeout = configured if timeout_sec is None else max(1.0, float(timeout_sec))
        deadline = time.monotonic() + timeout
        generation = self._tasks.change_generation()
        while True:
            result = self.poll_task_case_result(task_id, test_name)
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"judgehost task timed out after {int(timeout)}s")
            generation = self._tasks.wait_for_change(generation, remaining)

    def poll_task_case_result(
        self,
        task_id: str,
        test_name: str,
    ) -> CaseTerminalReport | None:
        return project_task_case_result(
            self._tasks,
            self._batch_runtime,
            task_id,
            test_name,
        )

    def wait_for_task(self, task_id: str, timeout_sec: float | None = None) -> str:
        result = self.wait_for_task_result(task_id, timeout_sec=timeout_sec)
        if result["task_status"] == "failed":
            raise RuntimeError(result["error"] or "judgehost task failed without error text")
        return result["run_id"]
