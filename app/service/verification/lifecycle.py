from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from app.service.platform.hashing import canonical_json
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.verification.execution_result import (
    ExecutionResult,
    normalize_execution_result,
)


AdmissionOutcome = Literal["admitted", "already-exists"]
ActivationOutcome = Literal["activated", "already-running", "closed", "missing"]
TransitionOutcome = Literal["transitioned", "closed", "missing"]
ParentTransition = Literal["", "failed", "ok", "sanity-running"]

TASK_GENERATE_INPUT = "generate-input"
TASK_MAIN_CORRECT = "main-correct"
TASK_SOLUTION_RUN = "solution-run"
PROGRAM_ACCEPTED = "accepted"
VerificationTaskKind = Literal[
    "generate-input",
    "main-correct",
    "solution-run",
]


@dataclass(frozen=True)
class VerificationCompileSpec:
    source_name: str
    source_file: PayloadFile
    extra_source_files: tuple[tuple[str, PayloadFile], ...] = ()
    manual_validate_only: bool = False

    def validate(self) -> None:
        if (
            not self.source_name
            or "/" in self.source_name
            or "\\" in self.source_name
            or self.source_name in {".", ".."}
        ):
            raise ValueError("verification compile source name is invalid")
        extra_names = tuple(name for name, _source in self.extra_source_files)
        if len(set(extra_names)) != len(extra_names):
            raise ValueError("verification compile spec repeats an extra source")
        if tuple(sorted(self.extra_source_files)) != self.extra_source_files:
            raise ValueError("verification compile extra sources are not canonical")
        if any(
            not name or "/" in name or "\\" in name or name in {".", ".."}
            for name in extra_names
        ):
            raise ValueError("verification compile extra source name is invalid")


@dataclass(frozen=True)
class VerificationProgram:
    program_id: str
    kind: VerificationTaskKind
    source_path: str
    compile_spec: VerificationCompileSpec
    expected_behavior: str


_TASK_ID_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GENERATOR_PROGRAM_RE = re.compile(r"^generator-(0|[1-9][0-9]*)$")
_SOLUTION_PROGRAM_RE = re.compile(r"^solution-(0|[1-9][0-9]*)$")


def _task_id_component(value: str, *, label: str) -> str:
    if not _TASK_ID_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"verification task {label} is not a path-safe token")
    return value


def verification_task_id(
    verification_id: str,
    program_id: str,
    test_name: str,
) -> str:
    """Build a task identity from its immutable position in the execution plan."""

    safe_verification_id = _task_id_component(
        verification_id,
        label="verification id",
    )
    safe_program_id = _task_id_component(program_id, label="program id")
    safe_test_name = _task_id_component(test_name, label="test name")
    return "vt~" + "~".join(
        (safe_verification_id, safe_program_id, safe_test_name)
    )


@dataclass(frozen=True)
class VerificationAdmission:
    verification_id: str
    problem_id: int
    workspace_id: int | None
    signature: str
    source_commit: str
    kind: str


@dataclass(frozen=True)
class AdmissionCommit:
    verification_id: str
    outcome: AdmissionOutcome


@dataclass(frozen=True)
class PlannedTask:
    task_id: str
    predecessor_task_id: str | None
    task_kind: VerificationTaskKind
    source_path: str
    program_id: str
    test_name: str
    expected_behavior: str
    result: ExecutionResult = ExecutionResult()


@dataclass(frozen=True)
class ActivationPlan:
    verification_id: str
    detail_json: str
    programs: tuple[VerificationProgram, ...]
    tasks: tuple[PlannedTask, ...]

    @classmethod
    def build(
        cls,
        verification_id: str,
        *,
        detail: dict[str, object],
        programs: tuple[VerificationProgram, ...] | list[VerificationProgram],
        tasks: tuple[PlannedTask, ...] | list[PlannedTask],
    ) -> "ActivationPlan":
        return cls(
            verification_id=verification_id,
            detail_json=canonical_json(dict(detail), ensure_ascii=False),
            programs=tuple(programs),
            tasks=tuple(tasks),
        )

    def detail(self) -> dict[str, object]:
        payload = json.loads(self.detail_json)
        if not isinstance(payload, dict):
            raise ValueError("verification activation detail must be an object")
        return cast(dict[str, object], payload)

    def validate(self) -> None:
        if not self.verification_id:
            raise ValueError("verification activation id is required")
        if not self.tasks:
            raise ValueError("verification activation graph cannot be empty")
        if not self.programs:
            raise ValueError("verification activation program set cannot be empty")
        program_by_id: dict[str, VerificationProgram] = {}
        for program in self.programs:
            if not program.program_id:
                raise ValueError("verification program id is required")
            if program.program_id in program_by_id:
                raise ValueError(
                    "verification activation contains duplicate program ids"
                )
            if not program.source_path:
                raise ValueError(
                    f"verification program {program.program_id} has no source"
                )
            if program.kind not in {
                TASK_GENERATE_INPUT,
                TASK_MAIN_CORRECT,
                TASK_SOLUTION_RUN,
            }:
                raise ValueError(
                    f"verification program {program.program_id} has unknown kind"
                )
            if (
                program.kind == TASK_GENERATE_INPUT
                and _GENERATOR_PROGRAM_RE.fullmatch(program.program_id) is None
            ):
                raise ValueError(
                    f"verification generator program {program.program_id} is invalid"
                )
            if (
                program.kind == TASK_MAIN_CORRECT
                and program.program_id != PROGRAM_ACCEPTED
            ):
                raise ValueError(
                    "verification main-correct program must use accepted identity"
                )
            if (
                program.kind == TASK_SOLUTION_RUN
                and _SOLUTION_PROGRAM_RE.fullmatch(program.program_id) is None
            ):
                raise ValueError(
                    f"verification solution program {program.program_id} is invalid"
                )
            program.compile_spec.validate()
            program_by_id[program.program_id] = program
        accepted_program = program_by_id.get(PROGRAM_ACCEPTED)
        if (
            accepted_program is None
            or accepted_program.kind != TASK_MAIN_CORRECT
        ):
            raise ValueError("verification activation requires accepted program")
        task_ids = [task.task_id for task in self.tasks]
        if any(not task_id for task_id in task_ids):
            raise ValueError("verification task id is required")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("verification activation graph contains duplicate task ids")
        known = set(task_ids)
        children_by_parent: dict[str, list[str]] = {}
        indegree = {task_id: 0 for task_id in task_ids}
        for task in self.tasks:
            if task.task_kind not in {
                TASK_GENERATE_INPUT,
                TASK_MAIN_CORRECT,
                TASK_SOLUTION_RUN,
            }:
                raise ValueError(
                    f"verification task {task.task_id} has unknown kind"
                )
            program = program_by_id.get(task.program_id)
            if program is None:
                raise ValueError(
                    f"verification task {task.task_id} has unknown program"
                )
            if (
                task.task_kind != program.kind
                or task.source_path != program.source_path
                or task.expected_behavior != program.expected_behavior
            ):
                raise ValueError(
                    f"verification task {task.task_id} does not match its program"
                )
            expected_task_id = verification_task_id(
                self.verification_id,
                task.program_id,
                task.test_name,
            )
            if task.task_id != expected_task_id:
                raise ValueError(
                    f"verification task {task.task_id} does not match its plan identity"
                )
            predecessor = task.predecessor_task_id
            if predecessor is None:
                continue
            if predecessor not in known:
                raise ValueError(
                    f"verification task {task.task_id} has unknown predecessor {predecessor}"
                )
            if predecessor == task.task_id:
                raise ValueError(
                    f"verification task {task.task_id} cannot depend on itself"
                )
            children_by_parent.setdefault(predecessor, []).append(task.task_id)
            indegree[task.task_id] += 1
        ready = [task_id for task_id, count in indegree.items() if count == 0]
        visited = 0
        while ready:
            task_id = ready.pop()
            visited += 1
            for child_id in children_by_parent.get(task_id, ()):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        if visited != len(task_ids):
            raise ValueError("verification activation graph contains a cycle")
        task_program_ids = {task.program_id for task in self.tasks}
        unused_program_ids = set(program_by_id).difference(task_program_ids)
        if unused_program_ids:
            raise ValueError(
                "verification activation contains a program without tasks"
            )
        self.detail()

    def ordered_tasks(self) -> tuple[PlannedTask, ...]:
        """Return parents before children for immediate SQLite foreign keys."""

        self.validate()
        by_id = {task.task_id: task for task in self.tasks}
        children_by_parent: dict[str, list[str]] = {}
        indegree = {task.task_id: 0 for task in self.tasks}
        for task in self.tasks:
            predecessor = task.predecessor_task_id
            if predecessor is None:
                continue
            children_by_parent.setdefault(predecessor, []).append(task.task_id)
            indegree[task.task_id] += 1
        ready = deque(
            task.task_id
            for task in self.tasks
            if indegree[task.task_id] == 0
        )
        ordered: list[PlannedTask] = []
        while ready:
            task_id = ready.popleft()
            ordered.append(by_id[task_id])
            for child_id in children_by_parent.get(task_id, ()):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)
        return tuple(ordered)


@dataclass(frozen=True)
class ActivationCommit:
    verification_id: str
    outcome: ActivationOutcome
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationTransitionCommit:
    verification_id: str
    outcome: TransitionOutcome
    status: str
    cancelled_task_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SanityFinish:
    verification_id: str
    detail_json: str

    @classmethod
    def build(
        cls,
        verification_id: str,
        *,
        detail: dict[str, object],
    ) -> "SanityFinish":
        return cls(
            verification_id=verification_id,
            detail_json=canonical_json(dict(detail), ensure_ascii=False),
        )

    def detail(self) -> dict[str, object]:
        payload = json.loads(self.detail_json)
        if not isinstance(payload, dict):
            raise ValueError("verification sanity detail must be an object")
        return cast(dict[str, object], payload)


@dataclass(frozen=True)
class StartupRecoverySummary:
    verification_ids: tuple[str, ...]
    cancelled_task_ids: tuple[str, ...]


class VerificationSnapshotRecord(TypedDict):
    id: str
    problem_id: int
    workspace_id: int | None
    signature: str
    source_commit: str
    kind: str
    status: str
    fail_reason: str
    created_at: str
    finished_at: str


class VerificationSnapshot(TypedDict):
    record: VerificationSnapshotRecord
    detail: dict[str, object]
    tasks: list[dict[str, object]]


def cancelled_task_result(reason: str) -> ExecutionResult:
    return normalize_execution_result(error=reason)
