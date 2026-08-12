from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, TypeVar

from app.service.repository.workspace import WorkspaceService

T = TypeVar("T")


class WorkspaceMutationConflict(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceMutationResult(Generic[T]):
    value: T
    status: dict[str, str | int | None]


class WorkspaceMutationService:
    def __init__(self, workspace_service: WorkspaceService):
        self._workspace_service = workspace_service

    def read_locked(self, workspace: Path, action: Callable[[], T]) -> WorkspaceMutationResult[T]:
        with self._workspace_service.workspace_lock(workspace):
            value = action()
            status = self._workspace_service.read_workspace_status(workspace)
        return WorkspaceMutationResult(value=value, status=status)

    def write_locked(self, workspace: Path, action: Callable[[], T]) -> WorkspaceMutationResult[T]:
        with self._workspace_service.workspace_lock(workspace):
            value = action()
            status = self._workspace_service.refresh_workspace_status_by_path(workspace) or self._workspace_service.read_workspace_status(workspace)
        return WorkspaceMutationResult(value=value, status=status)
