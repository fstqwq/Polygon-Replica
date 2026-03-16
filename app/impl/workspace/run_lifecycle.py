from __future__ import annotations


def run_lifecycle_status_label(status: str) -> str:
    if status == "done":
        return "Completed"
    if status == "running":
        return "In progress"
    if status in {"failed", "interrupted"}:
        return "Failed"
    if status == "skipped":
        return "Skipped"
    return "Pending"


def run_lifecycle_current_step(steps: list[dict[str, object]]) -> tuple[int, str]:
    if not steps:
        return (0, "-")
    for step in steps:
        if step["status"] in {"running", "failed", "interrupted"}:
            return (step["index"], step["title"])
    for step in steps:
        if step["status"] == "pending":
            return (step["index"], step["title"])
    last = steps[-1]
    return (last["index"], last["title"])


def run_lifecycle_current_step_fields(steps: list[dict[str, object]], current_step_index: int) -> tuple[str, str, str]:
    safe_index = max(0, current_step_index)
    for step in steps:
        if step["index"] == safe_index:
            return (step["status"], step["status_label"], step["detail"])
    return ("pending", run_lifecycle_status_label("pending"), "")


def verification_step_title(step_id: str) -> str:
    if step_id == "gen":
        return "Generate Inputs"
    if step_id == "val":
        return "Generate Outputs"
    if step_id == "run":
        return "Run Solutions"
    if step_id == "check":
        return "Check Expectations"
    if not step_id:
        return "Step"
    return step_id.replace("_", " ").replace("-", " ").title()
