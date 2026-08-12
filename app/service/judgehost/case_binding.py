from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CaseBinding:
    """Opaque durable identities attached to one Judgehost case."""

    execution_scope_id: str
    program_id: str
    task_id: str
    test_name: str


@dataclass(frozen=True)
class CaseArtifactBinding:
    test_name: str
    input_ref: str
    answer_ref: str


@dataclass(frozen=True)
class CaseArtifactSet:
    run_config_json: str
    cases: tuple[CaseArtifactBinding, ...]


class CaseBindingPort(Protocol):
    def load_artifacts(
        self,
        execution_scope_id: str,
        selected_tests: tuple[str, ...],
        *,
        limit: int,
    ) -> CaseArtifactSet: ...

    def bind_and_expose(
        self,
        bindings: tuple[CaseBinding, ...],
        *,
        run_id: str,
        judgehost_task_id: str,
        expose: Callable[[], None],
    ) -> bool: ...

    def unbind(
        self,
        task_id: str,
        *,
        judgehost_task_id: str,
    ) -> bool: ...
