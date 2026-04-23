from __future__ import annotations

import base64
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.main_util import enforce_textarea_max_bytes, write_upload_file_limited
from app.service.platform.file_type import looks_like_binary_file
from app.service.platform.workspace_path import (
    is_repository_answer_path,
    safe_workspace_path,
    validate_workspace_rel_path,
)
from app.service.repository.git import GitService
from app.service.repository.workspace import WorkspaceService


@dataclass(frozen=True)
class WorkspaceFileEntry:
    path: str
    is_dir: bool
    is_file: bool


@dataclass(frozen=True)
class WorkspaceFileList:
    base_path: str
    entries: list[WorkspaceFileEntry]
    truncated: bool


@dataclass(frozen=True)
class WorkspaceFilePayload:
    path: str
    is_dir: bool
    size_bytes: int
    media_type: str
    encoding: str
    content: str


@dataclass(frozen=True)
class WorkspaceFileView:
    path: str
    exists: bool
    is_dir: bool
    is_binary: bool
    is_pdf: bool
    media_type: str
    content: str
    content_truncated: bool


class WorkspaceFileService:
    def __init__(self, git_service: GitService, workspace_service: WorkspaceService):
        self._git_service = git_service
        self._workspace_service = workspace_service

    def normalize_path(self, raw: str | None, *, allow_empty: bool = False, require_allowed_root: bool = False) -> str:
        return validate_workspace_rel_path(raw, allow_empty=allow_empty, require_allowed_root=require_allowed_root)

    def _reject_repository_answer_path(self, normalized: str) -> None:
        if is_repository_answer_path(Path(normalized).parts):
            raise ValueError("repository answer files are not allowed")

    def list_entries(self, workspace: Path, raw_path: str, *, limit: int, require_allowed_root: bool) -> WorkspaceFileList:
        normalized = self.normalize_path(raw_path, allow_empty=True, require_allowed_root=require_allowed_root)
        entries, truncated = self._git_service.list_files_capped(workspace, normalized or ".", limit=limit)
        items: list[WorkspaceFileEntry] = []
        for entry in entries:
            try:
                safe_entry = self.normalize_path(entry, require_allowed_root=require_allowed_root)
            except ValueError:
                continue
            target = safe_workspace_path(workspace, safe_entry)
            items.append(
                WorkspaceFileEntry(
                    path=safe_entry,
                    is_dir=bool(target.exists() and target.is_dir()),
                    is_file=bool(target.exists() and target.is_file()),
                )
            )
        return WorkspaceFileList(base_path=normalized, entries=items, truncated=truncated)

    def list_paths(self, workspace: Path, *, limit: int, require_allowed_root: bool) -> tuple[list[str], bool]:
        listed = self.list_entries(workspace, "", limit=limit, require_allowed_root=require_allowed_root)
        return ([entry.path for entry in listed.entries], listed.truncated)

    def file_payload(self, workspace: Path, raw_path: str, *, require_allowed_root: bool) -> WorkspaceFilePayload:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        target = safe_workspace_path(workspace, normalized)
        if not target.exists() or target.is_symlink():
            raise FileNotFoundError("file not found")
        if target.is_dir():
            return WorkspaceFilePayload(path=normalized, is_dir=True, size_bytes=0, media_type="", encoding="", content="")
        raw = target.read_bytes()
        media_type = "application/octet-stream"
        content = ""
        encoding = "base64"
        try:
            content = raw.decode("utf-8")
            media_type = "text/plain; charset=utf-8"
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode("ascii")
        return WorkspaceFilePayload(
            path=normalized,
            is_dir=False,
            size_bytes=len(raw),
            media_type=media_type,
            encoding=encoding,
            content=content,
        )

    def file_view(self, workspace: Path, raw_path: str, *, char_limit: int, require_allowed_root: bool) -> WorkspaceFileView:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        target = safe_workspace_path(workspace, normalized)
        media_type = ""
        is_pdf = False
        is_binary = False
        content = ""
        content_truncated = False
        if target.exists() and target.is_file():
            media_type = mimetypes.guess_type(normalized)[0] or ""
            is_pdf = normalized.lower().endswith(".pdf") or media_type == "application/pdf"
            is_binary = is_pdf or looks_like_binary_file(target)
            if not is_binary:
                content, content_truncated = self._git_service.read_file_limited(workspace, normalized, char_limit)
        return WorkspaceFileView(
            path=normalized,
            exists=target.exists(),
            is_dir=bool(target.exists() and target.is_dir()),
            is_binary=is_binary,
            is_pdf=is_pdf,
            media_type=media_type,
            content=content,
            content_truncated=content_truncated,
        )

    def write_text(self, workspace: Path, raw_path: str, content: str, *, require_allowed_root: bool) -> str:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        self._reject_repository_answer_path(normalized)
        safe_content = enforce_textarea_max_bytes(content, label="file content")
        with self._workspace_service.workspace_lock(workspace):
            self._git_service.write_file(workspace, normalized, safe_content)
        return normalized

    def create_empty(self, workspace: Path, raw_path: str, *, require_allowed_root: bool) -> str:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        self._reject_repository_answer_path(normalized)
        with self._workspace_service.workspace_lock(workspace):
            self._git_service.write_file(workspace, normalized, "")
        return normalized

    async def upload_file(self, workspace: Path, raw_path: str, upload: UploadFile, *, require_allowed_root: bool) -> tuple[str, int]:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        self._reject_repository_answer_path(normalized)
        tmp_path: Path | None = None
        total_bytes = 0
        try:
            with self._workspace_service.workspace_lock(workspace):
                target = safe_workspace_path(workspace, normalized)
                if target.exists() and target.is_dir():
                    raise ValueError("upload target must be a file path")
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=f".upload-{target.name}.", suffix=".tmp", dir=str(target.parent))
                tmp_path = Path(tmp_name)
                try:
                    with os.fdopen(fd, "wb") as out:
                        total_bytes = await write_upload_file_limited(upload, out)
                    os.replace(tmp_path, target)
                    tmp_path = None
                except Exception:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)
                        tmp_path = None
                    raise
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return (normalized, total_bytes)

    def rename_path(self, workspace: Path, raw_old_path: str, raw_new_path: str, *, require_allowed_root: bool) -> tuple[str, str]:
        old_path = self.normalize_path(raw_old_path, require_allowed_root=require_allowed_root)
        new_path = self.normalize_path(raw_new_path, require_allowed_root=require_allowed_root)
        self._reject_repository_answer_path(new_path)
        with self._workspace_service.workspace_lock(workspace):
            self._git_service.rename_path(workspace, old_path, new_path)
        return (old_path, new_path)

    def delete_path(self, workspace: Path, raw_path: str, *, require_allowed_root: bool) -> str:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        with self._workspace_service.workspace_lock(workspace):
            self._git_service.delete_path(workspace, normalized)
        return normalized

    def download_path(self, workspace: Path, raw_path: str, *, require_allowed_root: bool) -> Path:
        normalized = self.normalize_path(raw_path, require_allowed_root=require_allowed_root)
        target = safe_workspace_path(workspace, normalized)
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError("file not found")
        return target
