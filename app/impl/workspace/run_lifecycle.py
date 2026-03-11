from __future__ import annotations

import re


def run_lifecycle_status_label(status: str) -> str:
    token = str(status or "").strip().lower()
    if token == "done":
        return "Completed"
    if token == "running":
        return "In progress"
    if token in {"failed", "interrupted"}:
        return "Failed"
    if token == "skipped":
        return "Skipped"
    return "Pending"


def run_lifecycle_current_step(steps: list[dict[str, object]]) -> tuple[int, str]:
    if not steps:
        return (0, "-")
    for step in steps:
        status = str(step.get("status") or "").strip().lower()
        if status in {"running", "failed", "interrupted"}:
            try:
                return (int(step.get("index") or 0), str(step.get("title") or "-"))
            except Exception:
                return (0, str(step.get("title") or "-"))
    for step in steps:
        status = str(step.get("status") or "").strip().lower()
        if status == "pending":
            try:
                return (int(step.get("index") or 0), str(step.get("title") or "-"))
            except Exception:
                return (0, str(step.get("title") or "-"))
    last = steps[-1]
    try:
        return (int(last.get("index") or 0), str(last.get("title") or "-"))
    except Exception:
        return (0, str(last.get("title") or "-"))


def run_lifecycle_current_step_fields(steps: list[dict[str, object]], current_step_index: int) -> tuple[str, str, str]:
    safe_index = max(0, int(current_step_index))
    for step in steps:
        try:
            step_index = int(step.get("index") or 0)
        except Exception:
            step_index = 0
        if step_index != safe_index:
            continue
        status = str(step.get("status") or "pending").strip().lower() or "pending"
        status_label = str(step.get("status_label") or run_lifecycle_status_label(status)).strip() or "pending"
        detail = str(step.get("detail") or "").strip()
        return (status, status_label, detail)
    return ("pending", run_lifecycle_status_label("pending"), "")


def normalize_verification_step_id(raw: object) -> str:
    token = str(raw or "").strip().lower()
    if not token:
        return ""
    normalized = re.sub(r"[^a-z0-9._-]+", "", token)
    if normalized in {"gen", "generate", "generation"}:
        return "gen"
    if normalized in {"val", "validate", "validation"}:
        return "val"
    if normalized in {"run", "execute"}:
        return "run"
    if normalized in {"check", "judge", "verify"}:
        return "check"
    return normalized


def verification_step_title(step_id: str) -> str:
    token = str(step_id or "").strip().lower()
    if token == "gen":
        return "Generate Inputs"
    if token == "val":
        return "Generate Outputs"
    if token == "run":
        return "Run Solutions"
    if token == "check":
        return "Check Expectations"
    if not token:
        return "Step"
    return token.replace("_", " ").replace("-", " ").strip().title()


def verification_failed_build_step_id(step_hint: str, step_ids: list[str]) -> str:
    hint = str(step_hint or "").strip().lower()
    if not step_ids:
        return ""
    if ("check" in hint or "judge" in hint or "expect" in hint) and "check" in step_ids:
        return "check"
    if (
        "val" in hint
        or "validator" in hint
        or "solve" in hint
        or "answer" in hint
        or "sample_output_validate" in hint
        or "interactor" in hint
        or "accepted" in hint
    ) and "val" in step_ids:
        return "val"
    if ("run" in hint or "execute" in hint or "submission" in hint) and "run" in step_ids:
        return "run"
    if ("gen" in hint or "test" in hint or "compile" in hint) and "gen" in step_ids:
        return "gen"
    return step_ids[0]


