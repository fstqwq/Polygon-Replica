from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.registry import JudgehostTaskRow


def summary_mapping(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("judgehost summary must be an object")
    summary: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError("judgehost summary keys must be strings")
        summary[key] = item
    return summary


def summary_text(summary: dict[str, object], key: str) -> str:
    value = summary.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"judgehost summary {key} must be a string")
    return value


def summary_compile_diagnostics(summary: dict[str, object]) -> list[dict[str, object]]:
    value = summary.get("compile_diagnostics")
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError("judgehost compile_diagnostics must be a list")
    return [summary_mapping(item) for item in value]


def summary_error_text(summary: dict[str, object]) -> str:
    for item in summary_compile_diagnostics(summary):
        message = summary_text(item, "message")
        if message:
            return message
    return summary_text(summary, "error")


def load_run_summary(
    tasks: JudgehostTaskRegistry,
    run_id: str,
    verification_id: str = "",
) -> dict[str, object]:
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
) -> dict[str, object]:
    summary = load_run_summary(tasks, run_id, verification_id)
    if summary:
        return summary
    row_summary = row["summary"].copy()
    if row_summary:
        return row_summary
    return summary_mapping(row["result"].get("summary"))
