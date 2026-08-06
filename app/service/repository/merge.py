from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.service.platform.fs.op import extract_git_archive
from app.service.platform.git_process import run_git
from app.service.platform.workspace_path import is_hidden_workspace_path
from app.service.repository.merge_diff import (
    MAX_DIFF_BYTES,
    MergeComparison,
    MergeDiffSide,
    compare_merge_files,
)
from app.service.repository.workspace import WorkspaceService, atomic_swap_workspace
from app.setting import Settings


@dataclass(frozen=True)
class MergeFile:
    path: str
    size: int
    sha256: str
    executable: bool
    content_kind: str


@dataclass(frozen=True)
class MergeEntry:
    entry_id: str
    group_id: str
    path: str
    current: MergeFile | None
    latest: MergeFile | None
    suggested: MergeFile | None


@dataclass(frozen=True)
class MergePreview:
    preview_id: str
    actor: str
    problem: str
    workspace: Path
    current_head: str
    latest_head: str
    workspace_fingerprint: str
    created_at: float
    root: Path
    entries: tuple[MergeEntry, ...]
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    suggested_entries: tuple[MergeEntry, ...]
    current_manifest: tuple[MergeFile, ...]
    latest_manifest: tuple[MergeFile, ...]
    suggested_manifest: tuple[MergeFile, ...]
    suggested_available: bool


class WorkspaceMergeService:
    PREVIEW_TTL_SEC = 30 * 60
    _UNDO_REF = "refs/polygon-replica/undo"

    def __init__(self, settings: Settings, workspace_service: WorkspaceService):
        self._settings = settings
        self._workspace_service = workspace_service
        self._root = settings.cache_root / "runtime" / "workspace-merges"
        self._lock = threading.Lock()
        self._previews: dict[str, MergePreview] = {}
        self._preview_by_workspace: dict[str, str] = {}

    @staticmethod
    def _git(workspace: Path, *args: str) -> str:
        proc = run_git(["git", "-C", str(workspace), *args])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "Git operation failed")
        return proc.stdout.strip()

    @classmethod
    def current_head(cls, workspace: Path) -> str:
        proc = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"])
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()

    @classmethod
    def _shared_origin(cls, workspace: Path) -> str:
        origin = cls._git(workspace, "remote", "get-url", "origin")
        if not origin:
            raise RuntimeError("shared revision source is missing")
        return origin

    @classmethod
    def latest_shared_head(cls, workspace: Path) -> str:
        origin = cls._shared_origin(workspace)
        proc = run_git(["git", "ls-remote", origin, "refs/heads/main"])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "failed to read the latest shared revision")
        line = next((row for row in proc.stdout.splitlines() if row.strip()), "")
        if not line:
            return ""
        return line.split()[0]

    @classmethod
    def shared_revision_advanced(cls, workspace: Path) -> bool:
        return cls.current_head(workspace) != cls.latest_shared_head(workspace)

    def has_active_preview(self, workspace: Path) -> bool:
        with self._lock:
            self._prune_expired_locked()
            preview_id = self._preview_by_workspace.get(str(workspace))
            return preview_id is not None and preview_id in self._previews

    def advance_clean_workspace(self, workspace: Path) -> bool:
        """Fast-forward a clean workspace without creating merge or undo state."""
        with self._workspace_service.workspace_lock(workspace):
            current_head = self.current_head(workspace)
            latest_head = self.latest_shared_head(workspace)
            if not latest_head or current_head == latest_head:
                return False
            with self._lock:
                self._prune_expired_locked()
                preview_id = self._preview_by_workspace.get(str(workspace))
                if preview_id is not None and preview_id in self._previews:
                    return False
            status = run_git(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ]
            )
            if status.returncode != 0:
                raise RuntimeError(status.stderr or status.stdout or "failed to inspect current files")
            if status.stdout.strip():
                return False
            self._git(workspace, "fetch", "--quiet", "origin", latest_head)
            if current_head:
                ancestor = run_git(
                    [
                        "git",
                        "-C",
                        str(workspace),
                        "merge-base",
                        "--is-ancestor",
                        current_head,
                        latest_head,
                    ]
                )
                if ancestor.returncode != 0:
                    return False
                self._git(workspace, "merge", "--ff-only", "--quiet", latest_head)
            else:
                self._git(workspace, "reset", "--hard", latest_head)
            if self.current_head(workspace) != latest_head:
                raise RuntimeError("failed to update current files to the latest shared version")
            self.clear_undo(workspace)
            return True

    @staticmethod
    def _is_hidden(rel: PurePosixPath) -> bool:
        return is_hidden_workspace_path(tuple(rel.parts))

    @classmethod
    def _visible_files(cls, root: Path) -> list[tuple[str, Path]]:
        rows: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current = Path(dirpath)
            safe_dirs: list[str] = []
            for name in sorted(dirnames):
                path = current / name
                rel = PurePosixPath(path.relative_to(root).as_posix())
                if cls._is_hidden(rel):
                    continue
                if path.is_symlink():
                    raise ValueError(f"merge does not support symbolic links: {rel.as_posix()}")
                safe_dirs.append(name)
            dirnames[:] = safe_dirs
            for name in sorted(filenames):
                path = current / name
                rel = PurePosixPath(path.relative_to(root).as_posix())
                if cls._is_hidden(rel):
                    continue
                if path.is_symlink():
                    raise ValueError(f"merge does not support symbolic links: {rel.as_posix()}")
                if path.is_file():
                    rows.append((rel.as_posix(), path))
        rows.sort(key=lambda row: row[0])
        return rows

    @staticmethod
    def _content_kind(size: int, preview: bytes) -> str:
        if size > MAX_DIFF_BYTES:
            return "large"
        if b"\0" in preview:
            return "binary"
        try:
            preview.decode("utf-8")
        except UnicodeDecodeError:
            return "binary"
        return "text"

    @classmethod
    def _copy_and_hash(cls, source: Path, target: Path) -> tuple[int, str, str]:
        digest = hashlib.sha256()
        size = 0
        preview = bytearray()
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, target.open("wb") as dst:
            while True:
                chunk = src.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if len(preview) <= MAX_DIFF_BYTES:
                    preview.extend(chunk[: MAX_DIFF_BYTES + 1 - len(preview)])
                dst.write(chunk)
        shutil.copystat(source, target, follow_symlinks=False)
        return size, digest.hexdigest(), cls._content_kind(size, bytes(preview))

    @classmethod
    def _capture_tree(cls, source: Path, target: Path) -> dict[str, MergeFile]:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        manifest: dict[str, MergeFile] = {}
        for rel, path in cls._visible_files(source):
            size, identity, content_kind = cls._copy_and_hash(path, target / rel)
            mode = path.stat().st_mode
            manifest[rel] = MergeFile(
                rel,
                size,
                identity,
                bool(mode & stat.S_IXUSR),
                content_kind,
            )
        return manifest

    @classmethod
    def _manifest(cls, root: Path) -> dict[str, MergeFile]:
        manifest: dict[str, MergeFile] = {}
        for rel, path in cls._visible_files(root):
            digest = hashlib.sha256()
            size = 0
            preview = bytearray()
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    if len(preview) <= MAX_DIFF_BYTES:
                        preview.extend(chunk[: MAX_DIFF_BYTES + 1 - len(preview)])
            manifest[rel] = MergeFile(
                rel,
                size,
                digest.hexdigest(),
                bool(path.stat().st_mode & stat.S_IXUSR),
                cls._content_kind(size, bytes(preview)),
            )
        return manifest

    @staticmethod
    def _fingerprint(head: str, manifest: dict[str, MergeFile]) -> str:
        digest = hashlib.sha256()
        digest.update(head.encode("ascii"))
        digest.update(b"\0")
        for path in sorted(manifest):
            row = manifest[path]
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(row.size).encode("ascii"))
            digest.update(b"\0")
            digest.update(row.sha256.encode("ascii"))
            digest.update(b"\0x" if row.executable else b"\0-")
        return digest.hexdigest()

    @staticmethod
    def _clear_visible_tree(root: Path) -> None:
        for child in list(root.iterdir()):
            rel = PurePosixPath(child.name)
            if is_hidden_workspace_path(tuple(rel.parts)):
                continue
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child)

    @classmethod
    def _copy_tree(cls, source: Path, target: Path) -> None:
        for rel, path in cls._visible_files(source):
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    @classmethod
    def _assert_shared_tree_has_no_symlinks(cls, repo: Path, revision: str) -> None:
        proc = run_git(["git", "-C", str(repo), "ls-tree", "-r", revision])
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "failed to inspect the latest shared revision")
        for line in proc.stdout.splitlines():
            if line.startswith("120000 "):
                path = line.split("\t", 1)[-1]
                raise ValueError(f"merge does not support symbolic links: {path}")

    @classmethod
    def _build_merge_repo(
        cls,
        current_tree: Path,
        shared_origin: str,
        latest_head: str,
        target: Path,
    ) -> bool:
        cls._git(target, "remote", "set-url", "origin", shared_origin)
        cls._git(target, "fetch", "origin", f"{latest_head}:refs/remotes/origin/main")
        cls._clear_visible_tree(target)
        cls._copy_tree(current_tree, target)
        cls._git(target, "config", "user.name", "Polygon Replica")
        cls._git(target, "config", "user.email", "merge@polygon-replica.local")
        cls._git(target, "add", "-f", "--all", "--", ".")
        cls._git(target, "commit", "--allow-empty", "-m", "Merge preview input")
        merge = run_git(
            [
                "git",
                "-C",
                str(target),
                "merge",
                "--no-commit",
                "--no-ff",
                "--allow-unrelated-histories",
                "refs/remotes/origin/main",
            ]
        )
        conflicts = run_git(["git", "-C", str(target), "ls-files", "-u"])
        if conflicts.returncode != 0:
            raise RuntimeError(conflicts.stderr or conflicts.stdout or "failed to inspect merge result")
        if conflicts.stdout.strip():
            return False
        if merge.returncode != 0:
            raise RuntimeError(merge.stderr or merge.stdout or "failed to build merge suggestion")
        return True

    @staticmethod
    def _group_entries(
        current: dict[str, MergeFile],
        latest: dict[str, MergeFile],
        suggested: dict[str, MergeFile],
    ) -> tuple[
        tuple[MergeEntry, ...],
        tuple[tuple[str, tuple[str, ...]], ...],
        tuple[MergeEntry, ...],
    ]:
        paths = sorted(path for path in set(current) | set(latest) if current.get(path) != latest.get(path))
        parent = {path: path for path in paths}

        def find(path: str) -> str:
            root = path
            while parent[root] != root:
                root = parent[root]
            while parent[path] != path:
                path, parent[path] = parent[path], root
            return root

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        by_lower: dict[str, str] = {}
        for path in paths:
            lowered = path.casefold()
            other = by_lower.get(lowered)
            if other is not None:
                union(path, other)
            else:
                by_lower[lowered] = path
        for path in paths:
            parts = PurePosixPath(path).parts
            for count in range(1, len(parts)):
                other = by_lower.get(PurePosixPath(*parts[:count]).as_posix().casefold())
                if other is not None:
                    union(path, other)

        grouped: dict[str, list[str]] = {}
        for path in paths:
            grouped.setdefault(find(path), []).append(path)
        ordered_groups = sorted((tuple(sorted(rows)) for rows in grouped.values()), key=lambda rows: rows[0])
        entries: list[MergeEntry] = []
        group_rows: list[tuple[str, tuple[str, ...]]] = []
        entry_number = 0
        for group_number, rows in enumerate(ordered_groups, start=1):
            group_id = f"g{group_number:04d}"
            group_rows.append((group_id, rows))
            for path in rows:
                entry_number += 1
                entries.append(
                    MergeEntry(
                        f"e{entry_number:04d}",
                        group_id,
                        path,
                        current.get(path),
                        latest.get(path),
                        suggested.get(path),
                    )
                )
        suggested_entries = tuple(
            MergeEntry(
                f"s{number:04d}",
                "",
                path,
                current.get(path),
                latest.get(path),
                suggested.get(path),
            )
            for number, path in enumerate(
                sorted(
                    path
                    for path in set(current) | set(suggested)
                    if current.get(path) != suggested.get(path)
                ),
                start=1,
            )
        )
        return tuple(entries), tuple(group_rows), suggested_entries

    def _drop_preview(self, preview: MergePreview) -> None:
        self._previews.pop(preview.preview_id, None)
        key = str(preview.workspace)
        if self._preview_by_workspace.get(key) == preview.preview_id:
            self._preview_by_workspace.pop(key, None)
        shutil.rmtree(preview.root, ignore_errors=True)

    def _prune_expired_locked(self) -> None:
        cutoff = time.time() - self.PREVIEW_TTL_SEC
        for preview in list(self._previews.values()):
            if preview.created_at < cutoff:
                self._drop_preview(preview)

    def start_preview(self, actor: str, problem: str, workspace: Path) -> MergePreview:
        preview_id = secrets.token_urlsafe(24)
        root = self._root / preview_id
        current_tree = root / "current"
        latest_tree = root / "latest"
        merge_repo = root / "suggested"
        self._root.mkdir(parents=True, exist_ok=True)
        workspace_size = self._tree_size(workspace)
        preview_required = workspace_size * 3
        preview_reserve = max(1024 * 1024 * 1024, preview_required // 10)
        if shutil.disk_usage(self._root).free < preview_required + preview_reserve:
            raise RuntimeError("not enough disk space to create a merge preview safely")
        root.mkdir(parents=True, exist_ok=False)
        try:
            with self._workspace_service.workspace_lock(workspace):
                current_head = self.current_head(workspace)
                latest_head = self.latest_shared_head(workspace)
                if not latest_head:
                    raise RuntimeError("there is no shared revision to merge")
                shared_origin = self._shared_origin(workspace)
                current = self._capture_tree(workspace, current_tree)
                merge_clone = run_git(
                    ["git", "clone", "--no-hardlinks", str(workspace), str(merge_repo)]
                )
                if merge_clone.returncode != 0:
                    raise RuntimeError(
                        merge_clone.stderr or merge_clone.stdout or "failed to create merge preview"
                    )
            fingerprint = self._fingerprint(current_head, current)
            clone = run_git(["git", "clone", "--no-checkout", shared_origin, str(root / "shared-repo")])
            if clone.returncode != 0:
                raise RuntimeError(clone.stderr or clone.stdout or "failed to read the latest shared revision")
            shared_repo = root / "shared-repo"
            self._assert_shared_tree_has_no_symlinks(shared_repo, latest_head)
            extract_git_archive(shared_repo, latest_head, latest_tree)
            latest = self._manifest(latest_tree)
            suggested_available = self._build_merge_repo(
                current_tree, shared_origin, latest_head, merge_repo
            )
            suggested = self._manifest(merge_repo) if suggested_available else {}
            entries, groups, suggested_entries = self._group_entries(current, latest, suggested)
            preview = MergePreview(
                preview_id,
                actor,
                problem,
                workspace,
                current_head,
                latest_head,
                fingerprint,
                time.time(),
                root,
                entries,
                groups,
                suggested_entries,
                tuple(current[path] for path in sorted(current)),
                tuple(latest[path] for path in sorted(latest)),
                tuple(suggested[path] for path in sorted(suggested)),
                suggested_available,
            )
            with self._lock:
                self._prune_expired_locked()
                old_id = self._preview_by_workspace.get(str(workspace))
                if old_id is not None:
                    old = self._previews.get(old_id)
                    if old is not None:
                        self._drop_preview(old)
                self._previews[preview_id] = preview
                self._preview_by_workspace[str(workspace)] = preview_id
            return preview
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def get_preview(self, actor: str, problem: str, preview_id: str) -> MergePreview:
        with self._lock:
            self._prune_expired_locked()
            preview = self._previews.get(preview_id)
        if preview is None or preview.actor != actor or preview.problem != problem:
            raise ValueError("merge preview is missing or expired")
        return preview

    def cancel_preview(self, actor: str, problem: str, preview_id: str) -> None:
        preview = self.get_preview(actor, problem, preview_id)
        with self._lock:
            self._drop_preview(preview)

    def _claim_preview(self, actor: str, problem: str, preview_id: str) -> MergePreview:
        with self._lock:
            self._prune_expired_locked()
            preview = self._previews.get(preview_id)
            if preview is None or preview.actor != actor or preview.problem != problem:
                raise ValueError("merge preview is missing or expired")
            self._previews.pop(preview_id)
            if self._preview_by_workspace.get(str(preview.workspace)) == preview_id:
                self._preview_by_workspace.pop(str(preview.workspace), None)
            return preview

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    @classmethod
    def _manual_result(cls, preview: MergePreview, choices: dict[str, str], target: Path) -> None:
        group_ids = {group_id for group_id, _paths in preview.groups}
        if set(choices) != group_ids or any(side not in {"current", "latest"} for side in choices.values()):
            raise ValueError("choose a result for every affected file")
        shutil.copytree(preview.root / "latest", target)
        for group_id, paths in preview.groups:
            if choices[group_id] == "latest":
                continue
            for rel in sorted(paths, key=lambda value: (value.count("/"), value), reverse=True):
                cls._remove_path(target / rel)
            for rel in paths:
                source = preview.root / "current" / rel
                if source.is_file():
                    destination = target / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

    @classmethod
    def _validate_result_tree(cls, root: Path) -> None:
        lowered: dict[str, str] = {}
        for rel, _path in cls._visible_files(root):
            key = rel.casefold()
            other = lowered.get(key)
            if other is not None and other != rel:
                raise ValueError(f"selected files collide by letter case: {other}, {rel}")
            lowered[key] = rel

    @staticmethod
    def _tree_size(root: Path) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    total += path.stat().st_size
        return total

    @classmethod
    def _disk_preflight(cls, workspace: Path, result_tree: Path) -> None:
        required = cls._tree_size(workspace) + cls._tree_size(result_tree)
        reserve = max(1024 * 1024 * 1024, required // 10)
        if shutil.disk_usage(workspace.parent).free < required + reserve:
            raise RuntimeError("not enough disk space to apply this merge safely")

    @staticmethod
    def _remove_git_operation_state(git_dir: Path) -> None:
        for name in ("rebase-merge", "rebase-apply", "sequencer"):
            shutil.rmtree(git_dir / name, ignore_errors=True)
        for name in ("MERGE_HEAD", "MERGE_MSG", "CHERRY_PICK_HEAD", "REVERT_HEAD", "index.lock"):
            (git_dir / name).unlink(missing_ok=True)

    @classmethod
    def _prepare_candidate(
        cls,
        workspace: Path,
        result_tree: Path,
        latest_head: str,
        mode: str,
        affected_count: int,
    ) -> Path:
        candidate = workspace.parent / f".{workspace.name}.merge-candidate-{uuid.uuid4().hex}"
        shutil.copytree(workspace, candidate, symlinks=True)
        try:
            git_dir = candidate / ".git"
            cls._remove_git_operation_state(git_dir)
            old_head = cls.current_head(candidate)
            if old_head:
                cls._git(candidate, "reset", "--mixed", "HEAD")
            cls._git(candidate, "config", "user.name", "Polygon Replica")
            cls._git(candidate, "config", "user.email", "merge@polygon-replica.local")
            cls._git(candidate, "add", "-f", "--all", "--", ".")
            reset_hidden = run_git(
                [
                    "git",
                    "-C",
                    str(candidate),
                    "reset",
                    "--quiet",
                    "--",
                    ":(glob).*",
                    ":(glob)**/.*",
                ]
            )
            if reset_hidden.returncode != 0:
                raise RuntimeError(reset_hidden.stderr or reset_hidden.stdout or "failed to prepare undo")
            cls._git(candidate, "commit", "--allow-empty", "-m", "Local merge undo snapshot")
            undo_commit = cls.current_head(candidate)
            cls._git(candidate, "update-ref", cls._UNDO_REF, undo_commit)
            cls._git(candidate, "fetch", "origin", latest_head)
            cls._git(candidate, "reset", "--hard", latest_head)
            cls._clear_visible_tree(candidate)
            cls._copy_tree(result_tree, candidate)
            post_fingerprint = cls._fingerprint(latest_head, cls._manifest(candidate))
            metadata_dir = git_dir / "polygon-replica"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            metadata = (
                f"undo_commit={undo_commit}\n"
                f"old_head={old_head}\n"
                f"post_head={latest_head}\n"
                f"post_fingerprint={post_fingerprint}\n"
                f"mode={mode}\n"
                f"affected_count={affected_count}\n"
            )
            (metadata_dir / "undo").write_text(metadata, encoding="utf-8")
            return candidate
        except Exception:
            shutil.rmtree(candidate, ignore_errors=True)
            raise

    @staticmethod
    def _undo_metadata(workspace: Path) -> dict[str, str]:
        path = workspace / ".git" / "polygon-replica" / "undo"
        if not path.is_file():
            raise ValueError("there is no merge to undo")
        rows: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                rows[key] = value
        required = {
            "undo_commit",
            "old_head",
            "post_head",
            "post_fingerprint",
            "mode",
            "affected_count",
        }
        if set(rows) != required:
            raise RuntimeError("merge undo metadata is invalid")
        if rows["mode"] not in {"suggested", "manual"}:
            raise RuntimeError("merge undo metadata is invalid")
        try:
            affected_count = int(rows["affected_count"])
        except ValueError as exc:
            raise RuntimeError("merge undo metadata is invalid") from exc
        if affected_count < 0:
            raise RuntimeError("merge undo metadata is invalid")
        return rows

    def has_undo(self, workspace: Path) -> bool:
        try:
            self._undo_metadata(workspace)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def undo_context(self, workspace: Path) -> dict[str, object] | None:
        try:
            metadata = self._undo_metadata(workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        return {
            "mode": metadata["mode"],
            "affected_count": int(metadata["affected_count"]),
        }

    def clear_undo(self, workspace: Path) -> None:
        run_git(["git", "-C", str(workspace), "update-ref", "-d", self._UNDO_REF])
        (workspace / ".git" / "polygon-replica" / "undo").unlink(missing_ok=True)

    def apply_preview(
        self,
        actor: str,
        problem: str,
        preview_id: str,
        mode: str,
        choices: dict[str, str],
    ) -> None:
        preview = self._claim_preview(actor, problem, preview_id)
        result_tree = preview.root / f"apply-{uuid.uuid4().hex}"
        candidate: Path | None = None
        try:
            if mode == "suggested":
                if not preview.suggested_available:
                    raise ValueError("a suggested result is not available; choose files one by one")
                result_tree.mkdir(parents=True)
                self._copy_tree(preview.root / "suggested", result_tree)
            elif mode == "manual":
                self._manual_result(preview, choices, result_tree)
            else:
                raise ValueError("select a merge result")
            self._validate_result_tree(result_tree)
            with self._workspace_service.workspace_lock(preview.workspace):
                current_head = self.current_head(preview.workspace)
                current_manifest = self._manifest(preview.workspace)
                if current_head != preview.current_head or self._fingerprint(current_head, current_manifest) != preview.workspace_fingerprint:
                    raise RuntimeError("your files changed after this preview; create a new merge preview")
                if self.latest_shared_head(preview.workspace) != preview.latest_head:
                    raise RuntimeError("a newer shared revision is available; create a new merge preview")
                self._disk_preflight(preview.workspace, result_tree)
                affected_count = (
                    len(preview.suggested_entries) if mode == "suggested" else len(preview.entries)
                )
                candidate = self._prepare_candidate(
                    preview.workspace,
                    result_tree,
                    preview.latest_head,
                    mode,
                    affected_count,
                )
                atomic_swap_workspace(preview.workspace, candidate)
                candidate = None
        finally:
            if candidate is not None:
                shutil.rmtree(candidate, ignore_errors=True)
            shutil.rmtree(result_tree, ignore_errors=True)
            shutil.rmtree(preview.root, ignore_errors=True)

    def undo(self, workspace: Path) -> None:
        candidate: Path | None = None
        with self._workspace_service.workspace_lock(workspace):
            metadata = self._undo_metadata(workspace)
            current_head = self.current_head(workspace)
            fingerprint = self._fingerprint(current_head, self._manifest(workspace))
            if current_head != metadata["post_head"] or fingerprint != metadata["post_fingerprint"]:
                raise RuntimeError("your files changed after the merge; undo is no longer safe")
            self._disk_preflight(workspace, workspace)
            candidate = workspace.parent / f".{workspace.name}.merge-candidate-{uuid.uuid4().hex}"
            shutil.copytree(workspace, candidate, symlinks=True)
            try:
                self._git(candidate, "reset", "--hard", metadata["undo_commit"])
                if metadata["old_head"]:
                    self._git(candidate, "reset", "--mixed", metadata["old_head"])
                else:
                    self._git(candidate, "read-tree", "--empty")
                    self._git(candidate, "update-ref", "-d", "refs/heads/main")
                    self._git(candidate, "symbolic-ref", "HEAD", "refs/heads/main")
                self._git(candidate, "update-ref", "-d", self._UNDO_REF)
                (candidate / ".git" / "polygon-replica" / "undo").unlink(missing_ok=True)
                atomic_swap_workspace(workspace, candidate)
                candidate = None
            finally:
                if candidate is not None:
                    shutil.rmtree(candidate, ignore_errors=True)

    def entry_file(
        self, actor: str, problem: str, preview_id: str, entry_id: str, side: str
    ) -> tuple[Path, MergeFile]:
        preview = self.get_preview(actor, problem, preview_id)
        entry = next(
            (
                row
                for row in (*preview.entries, *preview.suggested_entries)
                if row.entry_id == entry_id
            ),
            None,
        )
        if entry is None or side not in {"current", "latest", "suggested"}:
            raise ValueError("merge preview file is invalid")
        descriptor = {
            "current": entry.current,
            "latest": entry.latest,
            "suggested": entry.suggested,
        }[side]
        if descriptor is None:
            raise ValueError("this side does not contain the selected file")
        path = preview.root / side / entry.path
        if not path.is_file() or path.is_symlink():
            raise ValueError("merge preview file is missing")
        return path, descriptor

    @staticmethod
    def _change_kind(left: MergeFile | None, right: MergeFile | None) -> str:
        if left is None:
            return "added"
        if right is None:
            return "deleted"
        if left.sha256 != right.sha256:
            return "modified"
        if left.executable != right.executable:
            return "mode"
        return "unchanged"

    def comparison(
        self,
        actor: str,
        problem: str,
        preview_id: str,
        entry_id: str,
        target: str,
    ) -> MergeComparison:
        preview = self.get_preview(actor, problem, preview_id)
        if target not in {"latest", "suggested"}:
            raise ValueError("merge comparison target is invalid")
        if target == "suggested" and not preview.suggested_available:
            raise ValueError("a suggested result is not available")
        rows = preview.suggested_entries if target == "suggested" else preview.entries
        entry = next((row for row in rows if row.entry_id == entry_id), None)
        if entry is None:
            raise ValueError("merge preview file is invalid")
        right_descriptor = entry.suggested if target == "suggested" else entry.latest
        left_path = preview.root / "current" / entry.path if entry.current is not None else None
        right_path = preview.root / target / entry.path if right_descriptor is not None else None
        base_url = f"/problems/{problem}/merge/{preview_id}/file/{entry_id}"
        left_side = MergeDiffSide(
            "My current file",
            entry.current is not None,
            entry.current.size if entry.current is not None else 0,
            entry.current.executable if entry.current is not None else False,
            f"{base_url}?side=current" if entry.current is not None else "",
        )
        right_side = MergeDiffSide(
            "Suggested result" if target == "suggested" else "Latest shared file",
            right_descriptor is not None,
            right_descriptor.size if right_descriptor is not None else 0,
            right_descriptor.executable if right_descriptor is not None else False,
            f"{base_url}?side={target}" if right_descriptor is not None else "",
        )
        return compare_merge_files(
            path=entry.path,
            change_kind=self._change_kind(entry.current, right_descriptor),
            left_path=left_path,
            left_side=left_side,
            right_path=right_path,
            right_side=right_side,
        )
