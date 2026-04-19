from __future__ import annotations

import io
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import app.main_util as main_util
from app.service.platform.workspace_path import (
    ALLOWED_WORKSPACE_ROOT_NAMES,
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
)
from app.service.platform.zip_extract import extract_zip_entry_to_path_limited


@dataclass(frozen=True)
class WorkspaceDiff:
    uploads: list[str]
    deletes: list[str]
    same: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.uploads or self.deletes)

    def as_payload(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "uploads": self.uploads,
            "deletes": self.deletes,
            "same": self.same,
        }


def normalize_workspace_text_bytes(payload: bytes) -> tuple[bytes, bool]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return (payload, False)
    return (text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"), True)


def _zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0o170000
    return mode == stat.S_IFLNK


def _normalize_zip_entry_name(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").strip("/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid zip path: {raw}")
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if not parts:
        return ""
    if is_hidden_workspace_path(parts):
        raise ValueError(f"hidden zip path is not allowed: {raw}")
    if not is_allowed_workspace_root_path(parts):
        raise ValueError(f"zip path root is not allowed: {raw}")
    return PurePosixPath(*parts).as_posix()


def _safe_workspace_files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for root_name in sorted(ALLOWED_WORKSPACE_ROOT_NAMES):
        base = root / root_name
        if not base.exists() or base.is_symlink():
            continue
        if base.is_file():
            result[root_name] = normalize_workspace_text_bytes(base.read_bytes())[0]
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            keep_dirs: list[str] = []
            for dirname in sorted(dirnames):
                child = dir_root / dirname
                rel_parts = child.relative_to(root).parts
                if child.is_symlink() or is_hidden_workspace_path(rel_parts):
                    continue
                keep_dirs.append(dirname)
            dirnames[:] = keep_dirs
            for filename in sorted(filenames):
                child = dir_root / filename
                rel_parts = child.relative_to(root).parts
                if child.is_symlink() or is_hidden_workspace_path(rel_parts) or not child.is_file():
                    continue
                rel = child.relative_to(root).as_posix()
                result[rel] = normalize_workspace_text_bytes(child.read_bytes())[0]
    return result


def _safe_workspace_dirs(root: Path) -> list[str]:
    result: list[str] = []
    for root_name in sorted(ALLOWED_WORKSPACE_ROOT_NAMES):
        base = root / root_name
        if not base.exists() or base.is_symlink() or not base.is_dir():
            continue
        result.append(f"{root_name}/")
        for dirpath, dirnames, _filenames in os.walk(base, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            keep_dirs: list[str] = []
            for dirname in sorted(dirnames):
                child = dir_root / dirname
                rel_parts = child.relative_to(root).parts
                if child.is_symlink() or is_hidden_workspace_path(rel_parts):
                    continue
                keep_dirs.append(dirname)
                result.append(child.relative_to(root).as_posix() + "/")
            dirnames[:] = keep_dirs
    return sorted(set(result))


def _compare_file_maps(remote: dict[str, bytes], local: dict[str, bytes]) -> WorkspaceDiff:
    remote_paths = set(remote)
    local_paths = set(local)
    uploads = sorted(path for path in local_paths if path not in remote_paths or local[path] != remote[path])
    deletes = sorted(remote_paths - local_paths)
    same = sorted(path for path in remote_paths & local_paths if remote[path] == local[path])
    return WorkspaceDiff(uploads=uploads, deletes=deletes, same=same)


def _copy_safe_workspace_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for root_name in sorted(ALLOWED_WORKSPACE_ROOT_NAMES):
        child = src / root_name
        if not child.exists() or child.is_symlink():
            continue
        target = dst / root_name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False)
        elif child.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _clear_safe_workspace_roots(workspace: Path) -> None:
    for root_name in sorted(ALLOWED_WORKSPACE_ROOT_NAMES):
        child = workspace / root_name
        if not child.exists():
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


class WorkspaceArchiveService:
    def build_snapshot_zip(self, workspace: Path) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for dirname in _safe_workspace_dirs(workspace):
                archive.writestr(dirname, b"")
            for rel, payload in sorted(_safe_workspace_files(workspace).items()):
                archive.writestr(rel, payload)
        return buffer.getvalue()

    def extract_zip(self, payload: bytes, destination: Path, *, max_bytes: int | None = None) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        seen_files: set[str] = set()
        seen_dirs: set[str] = set()
        extracted_total = 0
        cap = main_util.UPLOAD_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                for info in archive.infolist():
                    if _zipinfo_is_symlink(info):
                        raise ValueError(f"zip symlink is not allowed: {info.filename}")
                    rel = _normalize_zip_entry_name(info.filename)
                    if not rel:
                        continue
                    if info.is_dir():
                        if rel in seen_files:
                            raise ValueError(f"zip path conflict: {rel}")
                        seen_dirs.add(rel)
                        (destination / rel).mkdir(parents=True, exist_ok=True)
                        continue
                    if rel in seen_files or rel in seen_dirs:
                        raise ValueError(f"zip path conflict: {rel}")
                    seen_files.add(rel)
                    target = destination / rel
                    extracted_total += extract_zip_entry_to_path_limited(
                        archive,
                        info,
                        target,
                        total_before=extracted_total,
                        max_file_bytes=cap,
                        max_total_bytes=cap,
                        display_name=rel,
                        entry_too_large_prefix="workspace archive entry is too large",
                        payload_too_large_prefix="workspace archive payload is too large at",
                        normalize_utf8_newlines=True,
                    )
        except OSError as exc:
            raise ValueError("invalid workspace archive layout") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("workspace archive is not a valid zip") from exc

    def compare_zip(self, workspace: Path, payload: bytes, *, max_bytes: int | None = None) -> WorkspaceDiff:
        with tempfile.TemporaryDirectory(prefix="polygon-workspace-compare-") as temp_name:
            local_root = Path(temp_name) / "local"
            self.extract_zip(payload, local_root, max_bytes=max_bytes)
            return _compare_file_maps(_safe_workspace_files(workspace), _safe_workspace_files(local_root))

    def apply_zip(self, workspace: Path, payload: bytes, *, max_bytes: int | None = None) -> WorkspaceDiff:
        with tempfile.TemporaryDirectory(prefix="polygon-workspace-apply-") as temp_name:
            temp_root = Path(temp_name)
            local_root = temp_root / "local"
            backup_root = temp_root / "backup"
            self.extract_zip(payload, local_root, max_bytes=max_bytes)
            diff = _compare_file_maps(_safe_workspace_files(workspace), _safe_workspace_files(local_root))
            _copy_safe_workspace_tree(workspace, backup_root)
            try:
                _clear_safe_workspace_roots(workspace)
                _copy_safe_workspace_tree(local_root, workspace)
            except Exception:
                _clear_safe_workspace_roots(workspace)
                _copy_safe_workspace_tree(backup_root, workspace)
                raise
            return diff
