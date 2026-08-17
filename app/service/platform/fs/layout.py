"""Configured storage roots and safe application-owned filesystem locators."""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.setting import Settings


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _is_within(root: Path, target: Path) -> bool:
    return root == target or root in target.parents


@dataclass(frozen=True)
class VerificationLayout:
    root: Path
    logs: Path


@dataclass(frozen=True)
class PreviewLayout:
    root: Path
    logs: Path
    statement_preview: Path


@dataclass(frozen=True)
class SourceBackupLayout:
    root: Path
    archive: Path
    sidecar: Path


@dataclass(frozen=True)
class StorageLayout:
    """The single owner of configured roots and paths derived from them."""

    database_path: Path
    bare_root: Path
    workspace_root: Path
    contest_source_root: Path
    artifacts_root: Path
    cache_root: Path
    backup_root: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> "StorageLayout":
        return cls(
            database_path=settings.db_path,
            bare_root=settings.bare_root,
            workspace_root=settings.workspace_root,
            contest_source_root=settings.contest_source_root,
            artifacts_root=settings.artifacts_root,
            cache_root=settings.cache_root,
            backup_root=settings.backup_root,
        )

    @property
    def cache_artifacts_root(self) -> Path:
        return self.cache_root / "artifacts"

    @property
    def runtime_root(self) -> Path:
        return self.cache_root / "runtime"

    @property
    def runtime_blob_root(self) -> Path:
        return self.runtime_root / "blobs"

    @property
    def verification_root(self) -> Path:
        return self.cache_artifacts_root / "verifications"

    @property
    def preview_root(self) -> Path:
        return self.cache_artifacts_root / "previews"

    @property
    def snapshot_root(self) -> Path:
        return self.runtime_root / "snapshots"

    @property
    def archive_upload_root(self) -> Path:
        return self.cache_root / "archive-uploads"

    @property
    def contest_import_draft_root(self) -> Path:
        return self.cache_root / "contest-import-drafts"

    @property
    def workspace_merge_root(self) -> Path:
        return self.runtime_root / "workspace-merges"

    @property
    def worker_history_path(self) -> Path:
        return self.runtime_root / "worker-queue-events.jsonl"

    @property
    def export_root(self) -> Path:
        return self.artifacts_root / "exports"

    @property
    def materialization_root(self) -> Path:
        return self.artifacts_root / "materializations"

    @property
    def workspace_snapshot_download_root(self) -> Path:
        return self.snapshot_root / "downloads"

    @property
    def contest_artifact_root(self) -> Path:
        return self.artifacts_root / "contests"

    @property
    def artifact_staging_root(self) -> Path:
        return self.artifacts_root / ".staging"

    @property
    def source_backup(self) -> SourceBackupLayout:
        root = self.backup_root / "source-backup"
        return SourceBackupLayout(
            root=root,
            archive=root / "latest.tar.gz",
            sidecar=root / "latest.tar.gz.sha256",
        )

    def validate(self) -> dict[str, Path]:
        """Reject unsafe or overlapping configured storage geometry."""

        configured = {
            "bare_root": self.bare_root.absolute(),
            "workspace_root": self.workspace_root.absolute(),
            "contest_source_root": self.contest_source_root.absolute(),
            "backup_root": self.backup_root.absolute(),
            "artifacts_root": self.artifacts_root.absolute(),
            "cache_root": self.cache_root.absolute(),
        }
        for name, root in configured.items():
            if root == Path(root.anchor):
                raise RuntimeError(f"refusing filesystem root: {name}={root}")
            if root.is_symlink():
                raise RuntimeError(f"filesystem root must not be a symlink: {name}={root}")
            if root.exists() and not root.is_dir():
                raise RuntimeError(f"filesystem root must be a directory: {name}={root}")
        resolved = {name: root.resolve() for name, root in configured.items()}
        root_items = list(resolved.items())
        for index, (left_name, left) in enumerate(root_items):
            for right_name, right in root_items[index + 1 :]:
                if _is_within(left, right) or _is_within(right, left):
                    raise RuntimeError(
                        f"filesystem roots overlap: {left_name}={left}, "
                        f"{right_name}={right}"
                    )

        configured_database = self.database_path.absolute()
        if configured_database.is_symlink():
            raise RuntimeError(
                f"database path must not be a symlink: {configured_database}"
            )
        if configured_database.exists() and not configured_database.is_file():
            raise RuntimeError(f"database path must be a file: {configured_database}")
        database = configured_database.resolve()
        for name, root in resolved.items():
            if _is_within(root, database) or _is_within(database, root):
                raise RuntimeError(
                    f"database path overlaps managed root: {name}={root}, "
                    f"database={database}"
                )
        return resolved

    def bare_repository(self, repo_name: str) -> Path:
        return self._safe_relative(self.bare_root, repo_name, field_name="repo_name")

    def workspace(self, username: str, problem_slug: str) -> Path:
        user = self._normalize_token(username, field_name="username")
        return self._safe_relative(
            self.workspace_root,
            f"{user}/{problem_slug}",
            field_name="problem_slug",
        )

    def snapshot_source(self, snapshot_id: str) -> Path:
        token = self._normalize_token(snapshot_id, field_name="snapshot_id")
        return self._safe_relative(
            self.snapshot_root,
            f"{token}/src",
            field_name="snapshot_id",
        )

    def workspace_snapshot_download(self, snapshot_id: str) -> Path:
        token = self._normalize_token(snapshot_id, field_name="snapshot_download_id")
        return self._safe_relative(
            self.workspace_snapshot_download_root,
            token,
            field_name="snapshot_download_id",
        )

    def materialization_archive(
        self,
        problem_id: int,
        source_commit: str,
    ) -> Path:
        if problem_id < 1:
            raise ValueError("problem_id has invalid format")
        commit = self._normalize_token(source_commit, field_name="source_commit")
        return self._safe_relative(
            self.materialization_root,
            f"{problem_id}/{commit}/native-package.zip",
            field_name="materialization",
        )

    def materialization_staging(self, materialization_id: str) -> Path:
        token = self._normalize_token(
            materialization_id,
            field_name="materialization_id",
        )
        return self._safe_relative(
            self.artifact_staging_root,
            token,
            field_name="materialization_id",
        )

    def staging_directory(self, name: str) -> Path:
        token = self._normalize_token(name, field_name="staging_name")
        return self._safe_relative(
            self.artifact_staging_root,
            token,
            field_name="staging_name",
        )

    def export_problem(self, problem_token: str) -> Path:
        token = self._normalize_token(problem_token, field_name="problem_token")
        return self._safe_relative(
            self.export_root,
            token,
            field_name="problem_token",
        )

    def export_directory(self, problem_token: str, export_id: str) -> Path:
        problem_root = self.export_problem(problem_token)
        token = self._normalize_token(export_id, field_name="export_id")
        return self._safe_relative(problem_root, token, field_name="export_id")

    def export_archive(
        self,
        problem_token: str,
        export_id: str,
        filename: str,
    ) -> Path:
        export_root = self.export_directory(problem_token, export_id)
        token = self._normalize_token(filename, field_name="export_filename")
        return self._safe_relative(export_root, token, field_name="export_filename")

    def contest_source(self, contest_slug: str) -> Path:
        return self._safe_relative(
            self.contest_source_root,
            contest_slug,
            field_name="contest_slug",
        )

    def contest_job(self, contest_slug: str, job_id: str) -> Path:
        contest = self._safe_relative(
            self.contest_artifact_root,
            contest_slug,
            field_name="contest_slug",
        )
        token = self._normalize_token(job_id, field_name="contest_job_id")
        return self._safe_relative(contest, token, field_name="contest_job_id")

    def contest_artifact(
        self,
        contest_slug: str,
        job_id: str,
        artifact_id: str,
    ) -> Path:
        job = self.contest_job(contest_slug, job_id)
        token = self._normalize_token(artifact_id, field_name="contest_artifact_id")
        return self._safe_relative(
            job,
            f"artifacts/{token}",
            field_name="contest_artifact_id",
        )

    def resolve_artifact(self, rel_path: str) -> Path:
        return self._safe_relative(
            self.artifacts_root,
            rel_path,
            field_name="artifact_rel_path",
        )

    def resolve_verification_root(self, verification_id: str) -> Path:
        token = self._normalize_token(verification_id, field_name="verification_id")
        return self._safe_relative(
            self.verification_root,
            token,
            field_name="verification_id",
        )

    def prepare_verification_root(self, verification_id: str) -> Path:
        path = self.resolve_verification_root(verification_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def verification_layout(self, verification_id: str) -> VerificationLayout:
        root = self.resolve_verification_root(verification_id)
        return VerificationLayout(root=root, logs=root / "logs")

    def prepare_verification_layout(self, verification_id: str) -> VerificationLayout:
        layout = self.verification_layout(verification_id)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.logs.mkdir(parents=True, exist_ok=True)
        return layout

    def resolve_preview_root(self, preview_id: str) -> Path:
        token = self._normalize_token(preview_id, field_name="preview_id")
        return self._safe_relative(self.preview_root, token, field_name="preview_id")

    def preview_layout(self, preview_id: str) -> PreviewLayout:
        root = self.resolve_preview_root(preview_id)
        return PreviewLayout(
            root=root,
            logs=root / "logs",
            statement_preview=root / "statement_preview",
        )

    def prepare_preview_layout(self, preview_id: str) -> PreviewLayout:
        layout = self.preview_layout(preview_id)
        layout.root.mkdir(parents=True, exist_ok=True)
        layout.logs.mkdir(parents=True, exist_ok=True)
        layout.statement_preview.mkdir(parents=True, exist_ok=True)
        return layout

    @staticmethod
    def _normalize_token(value: str, *, field_name: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError(f"{field_name} has invalid format")
        return value

    @staticmethod
    def _safe_relative(root: Path, value: str, *, field_name: str) -> Path:
        if "\\" in value:
            raise ValueError(f"{field_name} has invalid format")
        relative = PurePosixPath(value)
        if relative.is_absolute() or not relative.parts:
            raise ValueError(f"{field_name} has invalid format")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"{field_name} has invalid format")
        base = root.resolve()
        candidate = base
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink():
                raise ValueError(f"{field_name} crosses a symbolic link")
        target = candidate.resolve()
        if target == base or base not in target.parents:
            raise ValueError(f"{field_name} escapes configured root")
        return candidate
