from collections.abc import Mapping

from app.service.execution.model import JsonValue
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.registry import JudgehostTaskRow
from app.service.judgehost.task.result_model import TaskSummary


def summary_text(summary: Mapping[str, object], key: str) -> str:
    value = summary.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"judgehost summary {key} must be a string")
    return value


def summary_compile_diagnostics(summary: TaskSummary) -> list[dict[str, JsonValue]]:
    return [item.copy() for item in summary.get("compile_diagnostics", [])]


def summary_error_text(summary: TaskSummary) -> str:
    for item in summary_compile_diagnostics(summary):
        message = summary_text(item, "message")
        if message:
            return message
    return summary_text(summary, "error")


def load_run_summary(
    tasks: JudgehostTaskRegistry,
    run_id: str,
    verification_id: str = "",
) -> TaskSummary:
    if not run_id:
        return {}
    row = tasks.get_for_run(run_id)
    if row is None:
        return {}
    task_run_id = row["run_id"]
    task_verification_id = row["verification_id"]
    if task_run_id and (task_run_id != run_id or task_verification_id != verification_id):
        return load_run_summary(tasks, task_run_id, task_verification_id)
    return row["summary"].copy()


def task_summary_for_row(
    tasks: JudgehostTaskRegistry,
    row: JudgehostTaskRow,
    *,
    run_id: str,
    verification_id: str,
) -> TaskSummary:
    summary = load_run_summary(tasks, run_id, verification_id)
    if summary:
        return summary
    row_summary = row["summary"].copy()
    if row_summary:
        return row_summary
    return row["result"].get("summary", {}).copy()
