import json
from pathlib import Path
from typing import Callable

from tests.common import db
from app.main import runtime
from app.service.verification.lifecycle import (
    ActivationCommit,
    ActivationPlan,
    AdmissionCommit,
    PlannedTask,
    VerificationCompileSpec,
    VerificationAdmission,
    VerificationProgram,
)


def db_fetch_one(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.fetch_one(sql, values)


def db_fetch_all(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.fetch_all(sql, values)


def db_execute(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.execute(sql, values)


def db_write_transaction(func: Callable):
    return db.write_transaction(func)


def db_connection():
    return db.conn()


def admit_test_verification(
    *,
    verification_id: str,
    problem_id: int,
    workspace_id: int | None,
    signature: str = "",
    source_commit: str = "",
    kind: str = "all",
) -> AdmissionCommit:
    return runtime.verification_service.admit_verification(
        VerificationAdmission(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            signature=signature,
            source_commit=source_commit,
            kind=kind,
        )
    )


def activate_test_verification(
    verification_id: str,
    *,
    programs: list[VerificationProgram] | tuple[VerificationProgram, ...],
    tasks: list[PlannedTask] | tuple[PlannedTask, ...],
    detail: dict[str, object] | None = None,
) -> ActivationCommit:
    return runtime.verification_service.activate_verification(
        ActivationPlan.build(
            verification_id,
            detail=(
                {"verification_id": verification_id, "task_graph": True}
                if detail is None
                else detail
            ),
            programs=programs,
            tasks=tasks,
        )
    )


def verification_programs_for_tasks(
    tasks: list[PlannedTask] | tuple[PlannedTask, ...],
) -> list[VerificationProgram]:
    """Build explicit compile-bearing program fixtures from canonical tasks."""

    programs: list[VerificationProgram] = []
    task_by_program_id: dict[str, PlannedTask] = {}
    for task in tasks:
        existing = task_by_program_id.get(task.program_id)
        if existing is not None:
            if (
                existing.task_kind != task.task_kind
                or existing.source_path != task.source_path
                or existing.expected_behavior != task.expected_behavior
            ):
                raise AssertionError(
                    f"conflicting fixture program {task.program_id}"
                )
            continue
        task_by_program_id[task.program_id] = task
        programs.append(
            VerificationProgram(
                program_id=task.program_id,
                kind=task.task_kind,
                source_path=task.source_path,
                compile_spec=VerificationCompileSpec(
                    source_name=Path(task.source_path).name,
                    source_file=runtime.runtime_blob_store.put_bytes(
                        b"test verification program fixture\n"
                    ),
                ),
                expected_behavior=task.expected_behavior,
            )
        )
    return programs


def judgehost_fetch_case(service, case_id: int):
    return service.state.batch_scheduler.fetch_case(int(case_id))


def judgehost_fetch_batch(service, batch_id: int):
    return service.state.batch_scheduler.fetch_batch(int(batch_id))


def judgehost_cases_for_run(service, run_id: str):
    return service.state.batch_scheduler.cases_for_run(run_id)


def read_preview_summary(preview_id: str) -> dict[str, object]:
    row = db_fetch_one("SELECT summary_json FROM previews WHERE id=?", [str(preview_id).strip()])
    if row is None:
        return {}
    text = str(row["summary_json"] or "")
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_preview_summary(preview_id: str, summary: dict[str, object]) -> None:
    row = db_fetch_one("SELECT id FROM previews WHERE id=?", [str(preview_id).strip()])
    if row is None:
        raise AssertionError(f"preview row missing: {preview_id}")
    db_execute(
        "UPDATE previews SET summary_json=? WHERE id=?",
        [
            json.dumps(summary, ensure_ascii=True, separators=(",", ":")),
            str(preview_id).strip(),
        ],
    )


def read_contest_job_summary(contest_id: int, job_id: str) -> dict[str, object]:
    contest_row = db_fetch_one("SELECT slug FROM contests WHERE id=?", [int(contest_id)])
    if contest_row is None:
        return {}
    path = runtime.contest_service.job_root(str(contest_row["slug"]), str(job_id).strip()) / "summary.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_contest_job_summary(contest_id: int, job_id: str, summary: dict[str, object]) -> None:
    contest_row = db_fetch_one("SELECT slug FROM contests WHERE id=?", [int(contest_id)])
    if contest_row is None:
        raise AssertionError(f"contest missing: {contest_id}")
    path = runtime.contest_service.job_root(str(contest_row["slug"]), str(job_id).strip()) / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
