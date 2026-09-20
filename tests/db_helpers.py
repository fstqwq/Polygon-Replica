import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from app.db import SQLValue

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
from app.service.verification.types import VerificationDetail


@contextmanager
def _persisted_read() -> Iterator[sqlite3.Connection]:
    """Inspect durable state independently, including after runtime shutdown."""

    uri = db.path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def db_fetch_one(sql: str, params: Sequence[SQLValue] | None = None) -> sqlite3.Row | None:
    values = [] if params is None else list(params)
    with _persisted_read() as connection:
        return connection.execute(sql, values).fetchone()


def db_fetch_all(sql: str, params: Sequence[SQLValue] | None = None) -> list[sqlite3.Row]:
    values = [] if params is None else list(params)
    with _persisted_read() as connection:
        return connection.execute(sql, values).fetchall()


def db_execute(sql: str, params: Sequence[SQLValue] | None = None) -> None:
    values = [] if params is None else list(params)
    return db.execute(sql, values)


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
    detail: VerificationDetail | None = None,
) -> ActivationCommit:
    return runtime.verification_service.activate_verification(
        ActivationPlan.build(
            verification_id,
            detail={} if detail is None else detail,
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
                raise AssertionError(f"conflicting fixture program {task.program_id}")
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
