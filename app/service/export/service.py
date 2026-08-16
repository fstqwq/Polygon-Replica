"""Package export jobs and adapters for verified revisions."""

import os
import re
import shutil
import threading
import uuid
import zipfile
from pathlib import Path

from app.config import ConfigValues
from app.db import DB
from app.service.disk.export_store import (
    ExportJobRow,
    ExportStore,
    MaterializationPackageRow,
)
from app.service.export.adapters import (
    PackageAdapter,
    PackageAdapterRegistry,
    PackageFormat,
)
from app.service.platform.fs.layout import StorageLayout
from app.service.platform.fs.op import extract_git_archive, remove_symlinks
from app.service.platform.hashing import sha256_file
from app.service.platform.workspace_path import (
    is_allowed_workspace_root_path,
    is_hidden_workspace_path,
    is_repository_answer_path,
)
from app.service.problem_package.service import ProblemPackageService
from app.service.statement.tex_compile import TexCompileService

NATIVE_PACKAGE_FORMAT = "native"


class PackageBuildBusy(RuntimeError):
    """The same verified revision package build is already running."""


class ExportService:
    """Persist user-visible package jobs and cache their archives."""

    def __init__(
        self,
        db: DB,
        storage_layout: StorageLayout,
        tex_compile_service: TexCompileService,
        problem_package_service: ProblemPackageService,
        config_values: ConfigValues,
    ) -> None:
        self.storage_layout = storage_layout
        self.problem_package_service = problem_package_service
        self.package_adapters = PackageAdapterRegistry(
            config_values,
            tex_compile_service,
        )
        self._job_formats = (
            NATIVE_PACKAGE_FORMAT,
            *self.package_adapters.formats,
        )
        self._store = ExportStore(
            db,
            job_formats=self._job_formats,
            package_formats=self.package_adapters.formats,
        )
        self._package_locks_guard = threading.Lock()
        self._package_locks: dict[tuple[str, str], threading.Lock] = {}

    @property
    def package_formats(self) -> tuple[PackageFormat, ...]:
        return self.package_adapters.formats

    def require_job_format(self, package_format: str) -> str:
        if package_format == NATIVE_PACKAGE_FORMAT:
            return NATIVE_PACKAGE_FORMAT
        return self.package_adapters.require_format(package_format)

    def package_format_display_name(self, package_format: str) -> str:
        if package_format == NATIVE_PACKAGE_FORMAT:
            return "Native"
        return self.package_adapters.require(package_format).display_name

    def _package_lock(
        self,
        verified_revision_id: str,
        package_format: PackageFormat,
    ) -> threading.Lock:
        key = (verified_revision_id, package_format)
        with self._package_locks_guard:
            return self._package_locks.setdefault(key, threading.Lock())

    def export_archive_path(
        self,
        problem_id: int,
        export_id: str,
        filename: str,
    ) -> Path | None:
        row = self._store.export_archive_row(int(problem_id), export_id)
        if row is None:
            return None
        stored_filename = Path(row["filename"]).name
        if not stored_filename or stored_filename != Path(filename).name:
            return None
        try:
            root = self.storage_layout.artifacts_root.resolve()
            candidate = self.storage_layout.resolve_artifact(row["archive_rel_path"])
            if root not in candidate.parents:
                return None
        except (OSError, ValueError):
            return None
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or int(candidate.stat().st_size) != row["size_bytes"]
            or sha256_file(candidate) != row["sha256"]
        ):
            return None
        return candidate

    def problem_export_jobs(
        self,
        problem_id: int,
        *,
        limit: int,
    ) -> list[ExportJobRow]:
        return self._store.problem_export_jobs(int(problem_id), limit=limit)

    def materialization_packages(
        self,
        problem_id: int,
        materialization_ids: list[str],
    ) -> list[MaterializationPackageRow]:
        return self._store.materialization_packages(
            int(problem_id),
            materialization_ids,
        )

    def latest_succeeded_export_job(
        self,
        problem_id: int,
        source_commit: str,
        package_format: str,
    ) -> ExportJobRow | None:
        return self._store.latest_succeeded_export_job(
            int(problem_id),
            source_commit,
            package_format,
        )

    def export_job(self, problem_id: int, job_id: str) -> ExportJobRow | None:
        return self._store.export_job(int(problem_id), job_id)

    def export_problem(self, export_id: str) -> dict[str, object] | None:
        return self._store.export_problem(export_id)

    def create_export_job(
        self,
        *,
        job_id: str,
        problem_id: int,
        actor_user_id: int,
        package_format: str,
        source_commit: str,
    ) -> None:
        resolved_format = self.require_job_format(package_format)
        self._store.create_export_job(
            job_id=job_id,
            problem_id=int(problem_id),
            actor_user_id=int(actor_user_id),
            export_type=resolved_format,
            source_commit=source_commit,
        )

    def mark_export_job_running(self, job_id: str, *, source_commit: str) -> None:
        self._store.mark_export_job_running(job_id, source_commit=source_commit)

    def mark_export_job_packaging(
        self,
        job_id: str,
        *,
        verified_revision_id: str,
    ) -> None:
        self._store.mark_export_job_packaging(
            job_id,
            materialization_id=verified_revision_id,
        )

    def mark_export_job_succeeded(
        self,
        job_id: str,
        *,
        verified_revision_id: str,
        export_id: str | None,
        warning: str,
    ) -> None:
        self._store.mark_export_job_succeeded(
            job_id,
            materialization_id=verified_revision_id,
            export_id=export_id,
            warning=warning,
        )

    def mark_export_job_failed(self, job_id: str, error: str) -> None:
        self._store.mark_export_job_failed(job_id, error)

    def fail_interrupted_export_jobs(self) -> int:
        return self._store.fail_interrupted_export_jobs()

    @staticmethod
    def job_phase(job: ExportJobRow) -> str:
        status = job["status"]
        if status == "queued":
            return "queued"
        if status == "succeeded":
            return "complete"
        if not job["started_at"]:
            return "queued"
        if job["materialization_id"]:
            return "packaging"
        return "verifying"

    @staticmethod
    def _archive_slug(value: str) -> str:
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
        return token or "problem"

    @classmethod
    def _public_problem_slug(cls, value: str) -> str:
        leaf = value.replace("\\", "/").strip("/").rsplit("/", 1)[-1]
        return cls._archive_slug(leaf)

    def _export_path(self, problem_slug: str, export_id: str, filename: str) -> Path:
        return self.storage_layout.export_archive(
            self._archive_slug(problem_slug),
            export_id,
            filename,
        )

    def _cached_export_path(
        self,
        *,
        problem_id: int,
        materialization_id: str,
        package_format: PackageFormat,
    ) -> tuple[str, Path] | None:
        export_id = self._store.cached_export(
            materialization_id=materialization_id,
            export_type=package_format,
        )
        if not export_id:
            return None
        row = self._store.export_archive_row(problem_id, export_id)
        if row is not None:
            path = self.export_archive_path(problem_id, export_id, row["filename"])
            if path is not None:
                return (export_id, path)
        self._store.delete_export(export_id)
        return None

    @staticmethod
    def _make_archive(target: Path, root: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_root = root.resolve()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for directory, directories, filenames in os.walk(
                root,
                topdown=True,
                followlinks=False,
            ):
                current = Path(directory)
                current_resolved = current.resolve()
                if (
                    current_resolved != resolved_root
                    and resolved_root not in current_resolved.parents
                ):
                    directories[:] = []
                    continue
                directories[:] = sorted(
                    name for name in directories if not (current / name).is_symlink()
                )
                for filename in sorted(filenames):
                    source = current / filename
                    if source.is_symlink() or not source.is_file():
                        raise ValueError(
                            f"package adapter produced a special file: {source}"
                        )
                    resolved = source.resolve()
                    if resolved_root not in resolved.parents:
                        raise ValueError(f"package adapter escaped staging: {source}")
                    archive.write(source, source.relative_to(root).as_posix())

    def create_export(
        self,
        problem: str,
        package_format: str,
        *,
        verified_revision_id: str,
        expected_archive_sha256: str | None = None,
    ) -> tuple[str, Path, str]:
        adapter = self.package_adapters.require(package_format)
        resolved_format = adapter.format
        lock = self._package_lock(verified_revision_id, resolved_format)
        if not lock.acquire(blocking=False):
            raise PackageBuildBusy("package build already running")
        try:
            return self._create_export(
                problem,
                adapter,
                verified_revision_id=verified_revision_id,
                expected_archive_sha256=expected_archive_sha256,
            )
        finally:
            lock.release()

    def _create_export(
        self,
        problem: str,
        adapter: PackageAdapter,
        *,
        verified_revision_id: str,
        expected_archive_sha256: str | None,
    ) -> tuple[str, Path, str]:
        package_format = adapter.format
        problem_row = self._store.problem_export_row(problem)
        if problem_row is None:
            raise ValueError(f"unknown problem: {problem}")
        verified_revision = self.problem_package_service.verified_revision(
            verified_revision_id
        )
        if (
            verified_revision is None
            or verified_revision["problem_id"] != problem_row["id"]
        ):
            raise ValueError("verified revision does not belong to the problem")
        export_id = f"e-{uuid.uuid4().hex[:10]}"
        public_slug = self._public_problem_slug(problem_row["slug"])
        filename = (
            f"{public_slug}-{package_format}-v"
            f"{verified_revision['revision_number']}.zip"
        )
        staging = self.storage_layout.staging_directory(
            f"export-{export_id}-{uuid.uuid4().hex}"
        )
        package_root = staging / "package"
        archive_partial = staging / f"{filename}.partial"
        output: Path | None = None
        published = False
        try:
            with self.problem_package_service.open_reader(
                verified_revision_id,
                expected_archive_sha256=expected_archive_sha256,
            ) as reader:
                adapter_plan = adapter.plan(reader)
                cached = self._cached_export_path(
                    problem_id=problem_row["id"],
                    materialization_id=verified_revision_id,
                    package_format=package_format,
                )
                if cached is not None:
                    return (*cached, adapter_plan.warning)
                adapter.build(
                    reader,
                    target=package_root,
                    canonical_problem_slug=problem_row["slug"],
                    short_name=(
                        public_slug if adapter.accepts_short_name else None
                    ),
                    plan=adapter_plan,
                )
                self._make_archive(archive_partial, package_root)
                output = self._export_path(problem_row["slug"], export_id, filename)
                output.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archive_partial, output)
                self._store.insert_export_record(
                    export_id=export_id,
                    problem_id=problem_row["id"],
                    materialization_id=verified_revision_id,
                    export_type=package_format,
                    filename=filename,
                    archive_rel_path=output.relative_to(
                        self.storage_layout.artifacts_root
                    ).as_posix(),
                    sha256=sha256_file(output),
                    size_bytes=int(output.stat().st_size),
                    source_commit=verified_revision["source_commit"],
                )
                published = True
            if output is None:
                raise RuntimeError("package adapter did not publish an archive")
            return (export_id, output, adapter_plan.warning)
        except Exception:
            if published:
                self._store.delete_export(export_id)
                if output is not None:
                    output.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _copy_workspace_tree(source: Path, target: Path, *, root: Path) -> None:
        for child in source.iterdir():
            relative = child.relative_to(root)
            if relative.parts and relative.parts[0] in {"temp", "draft"}:
                continue
            if (
                is_hidden_workspace_path(relative.parts)
                or not is_allowed_workspace_root_path(relative.parts)
                or is_repository_answer_path(relative.parts)
                or child.is_symlink()
            ):
                continue
            destination = target / child.name
            if child.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                ExportService._copy_workspace_tree(child, destination, root=root)
            elif child.is_file():
                shutil.copy2(child, destination)

    def create_workspace_snapshot(
        self,
        problem: str,
        *,
        workspace_id: int,
        source_commit: str | None = None,
        revision_number: int | None = None,
    ) -> Path:
        if (source_commit is None) != (revision_number is None):
            raise ValueError("snapshot revision identity is incomplete")
        if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise ValueError("snapshot source commit is invalid")
        if revision_number is not None and revision_number < 1:
            raise ValueError("snapshot revision number is invalid")
        problem_row = self._store.problem_export_row(problem)
        workspace_row = self._store.workspace_export_context(int(workspace_id))
        if problem_row is None or workspace_row is None:
            raise ValueError("workspace snapshot context is unavailable")
        workspace = Path(workspace_row["path"]).resolve()
        expected = self.storage_layout.workspace(workspace_row["username"], problem_row["slug"])
        if workspace != expected or not workspace.is_dir() or not (workspace / ".git").is_dir():
            raise ValueError("workspace snapshot source is unavailable")
        parent = self.storage_layout.workspace_snapshot_download(
            f"snap-{uuid.uuid4().hex[:12]}"
        )
        work = parent / "work"
        package_root = work / self._archive_slug(problem_row["slug"])
        source = work / "_source"
        try:
            package_root.mkdir(parents=True)
            if source_commit is None:
                source.mkdir(parents=True)
                self._copy_workspace_tree(workspace, source, root=workspace)
            else:
                extract_git_archive(workspace, source_commit, source, timeout=120)
                remove_symlinks(source)
            self._copy_workspace_tree(source, package_root, root=source)
            suffix = f"-v{revision_number}" if revision_number is not None else ""
            archive = shutil.make_archive(
                str(parent / f"{self._archive_slug(problem_row['slug'])}{suffix}-snapshot"),
                "zip",
                root_dir=work,
                base_dir=package_root.name,
            )
            return Path(archive)
        except Exception:
            shutil.rmtree(parent, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
