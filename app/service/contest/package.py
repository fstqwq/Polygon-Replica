"""Prepare immutable Contest package inputs and assemble cached exports."""

import os
import shutil
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.service.contest.naming import problem_slug_file_token
from app.service.contest.service import ContestService
from app.service.export.adapters import (
    ContestPackagePlacement,
    PackageAdapterRegistry,
    PackageFormat,
)
from app.service.export.adapters.shared import statement_language_code
from app.service.export.service import CachedExternalPackage
from app.service.importing.archive import ArchiveView, problem_archive_policy
from app.service.platform.zip_process import create_zip_archive
from app.service.problem_package.service import ProblemPackageService
from app.service.statement.context import statement_language_sort_key


@dataclass(frozen=True)
class ContestPackageItem:
    contest_problem_id: int
    ordinal: int
    idx: str
    problem_id: int
    problem_slug: str
    statement_folder: str
    source_commit: str
    revision_number: int
    native_package_id: str
    archive_sha256: str


@dataclass(frozen=True)
class ContestPackageSnapshot:
    contest_id: int
    contest_slug: str
    source_generation: int
    package_format: PackageFormat
    items: tuple[ContestPackageItem, ...]
    statement_languages: tuple[str, ...]


@dataclass(frozen=True)
class ContestPackageDownload:
    path: Path
    filename: str
    cleanup_root: Path

    def close(self) -> None:
        shutil.rmtree(self.cleanup_root, ignore_errors=True)


class ContestPackageService:
    """Assemble one Contest bundle from ready cached external packages."""

    def __init__(
        self,
        contest_service: ContestService,
        package_adapters: PackageAdapterRegistry,
        problem_package_service: ProblemPackageService,
        *,
        problem_zip_max_expanded_bytes: int,
    ) -> None:
        self._contest = contest_service
        self._package_adapters = package_adapters
        self._problem_packages = problem_package_service
        self._problem_zip_max_expanded_bytes = max(
            1,
            int(problem_zip_max_expanded_bytes),
        )

    def freeze_download(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        package_format: str,
    ) -> ContestPackageSnapshot:
        safe_format = self._package_adapters.require_format(package_format)
        contest = self._contest.contest_context(contest_slug)
        if contest is None or int(contest["id"]) != int(contest_id):
            raise ValueError("Contest is unavailable")
        roster = self._contest.contest_problems(contest_id)
        if not roster:
            raise ValueError("Contest has no problems")
        readiness = self._problem_packages.published_readiness_many(
            [int(row["problem_id"]) for row in roster]
        )
        blocked = [
            str(row["problem_slug"])
            for row in roster
            if readiness[int(row["problem_id"])]["status"] != "ready"
        ]
        if blocked:
            raise ValueError("Packages are not ready: " + ", ".join(blocked))

        items: list[ContestPackageItem] = []
        language_sets: list[set[str]] = []
        for ordinal, row in enumerate(roster, start=1):
            package = readiness[int(row["problem_id"])]
            native_package_id = str(package["native_package_id"] or "")
            native_package = self._problem_packages.native_package(
                native_package_id
            )
            if (
                native_package is None
                or native_package["status"] != "available"
                or native_package["source_commit"] != package["published_commit"]
            ):
                raise ValueError(
                    f"Package is no longer available: {row['problem_slug']}"
                )
            items.append(
                ContestPackageItem(
                    contest_problem_id=int(row["contest_problem_id"]),
                    ordinal=ordinal,
                    idx=str(row["idx"]),
                    problem_id=int(row["problem_id"]),
                    problem_slug=str(row["problem_slug"]),
                    statement_folder=str(row["statement_folder"]),
                    source_commit=native_package["source_commit"],
                    revision_number=native_package["revision_number"],
                    native_package_id=native_package["id"],
                    archive_sha256=native_package["archive_sha256"],
                )
            )
            language_sets.append(
                set(
                    self._problem_packages.statement_languages(
                        native_package["id"]
                    )
                )
            )
        common_languages = set.intersection(*language_sets)
        if not common_languages:
            raise ValueError("Contest problems have no common statement language")
        languages = tuple(
            sorted(common_languages, key=statement_language_sort_key)
        )
        return ContestPackageSnapshot(
            contest_id=int(contest_id),
            contest_slug=contest_slug,
            source_generation=int(contest["source_generation"]),
            package_format=safe_format,
            items=tuple(items),
            statement_languages=languages,
        )

    def validate_snapshot(self, snapshot: ContestPackageSnapshot) -> None:
        current = self.freeze_download(
            contest_id=snapshot.contest_id,
            contest_slug=snapshot.contest_slug,
            package_format=snapshot.package_format,
        )
        if current != snapshot:
            raise ValueError("Contest or Published Packages changed; retry download")

    def _extract_external_package(self, source: Path, target: Path) -> None:
        policy = problem_archive_policy(self._problem_zip_max_expanded_bytes)
        try:
            with ArchiveView(source, policy) as archive:
                for relative, info in sorted(archive.entries.items()):
                    destination = target.joinpath(*PurePosixPath(relative).parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        archive.zip_file.copy_to(info, destination)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ValueError(
                f"cached external package is invalid: {source.name}: {exc}"
            ) from exc

    @staticmethod
    def _checked_statement_pdf(path: Path, *, language: str) -> Path:
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"Contest {language} statement PDF is unavailable"
            )
        return path

    def build_download(
        self,
        snapshot: ContestPackageSnapshot,
        *,
        external_packages: Mapping[int, CachedExternalPackage],
        statement_pdfs: Mapping[str, Path],
    ) -> ContestPackageDownload:
        expected_problem_ids = {item.problem_id for item in snapshot.items}
        if set(external_packages) != expected_problem_ids:
            raise ValueError("Contest external package set is incomplete")
        if set(statement_pdfs) != set(snapshot.statement_languages):
            raise ValueError("Contest statement PDF set is incomplete")

        operation_id = f"download-{uuid.uuid4().hex[:12]}"
        operation_root = self._contest.package_download_root(
            snapshot.contest_slug,
            operation_id,
        )
        staging = operation_root / ".bundle-staging"
        package_roots = staging / "package-roots"
        packages_dir = staging / "packages"
        package_roots.mkdir(parents=True, exist_ok=False)
        packages_dir.mkdir(parents=True, exist_ok=False)
        staged_archive = staging / (
            f"{snapshot.contest_slug}-{snapshot.package_format}-packages.zip"
        )
        final_archive = operation_root / staged_archive.name
        adapter = self._package_adapters.require(snapshot.package_format)
        package_archives: list[Path] = []
        try:
            for item in snapshot.items:
                cached = external_packages[item.problem_id]
                if (
                    cached.native_package_id != item.native_package_id
                    or cached.package_format != snapshot.package_format
                ):
                    raise ValueError(
                        f"cached external package changed: {item.problem_slug}"
                    )
                package_root = package_roots / str(item.contest_problem_id)
                package_root.mkdir(parents=True, exist_ok=False)
                self._extract_external_package(cached.path, package_root)
                adapter.apply_contest_placement(
                    package_root,
                    canonical_problem_slug=item.problem_slug,
                    placement=ContestPackagePlacement(
                        idx=item.idx,
                        ordinal=item.ordinal,
                    ),
                )
                token = problem_slug_file_token(item.problem_slug)
                filename = (
                    f"{item.idx}-{token}.zip" if item.idx else f"{token}.zip"
                )
                package_archive = packages_dir / filename
                create_zip_archive(package_root, package_archive)
                package_archives.append(package_archive.resolve())

            language_files: list[tuple[str, Path]] = []
            seen_codes: set[str] = set()
            for language in snapshot.statement_languages:
                code = statement_language_code(language)
                if code in seen_codes:
                    raise ValueError(
                        f"duplicate Contest statement language code: {code}"
                    )
                seen_codes.add(code)
                language_files.append(
                    (
                        f"statements.{code}.pdf",
                        self._checked_statement_pdf(
                            statement_pdfs[language],
                            language=language,
                        ),
                    )
                )

            with zipfile.ZipFile(
                staged_archive,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                for filename, statement_pdf in language_files:
                    archive.write(statement_pdf, arcname=filename)
                for package_archive in package_archives:
                    archive.write(
                        package_archive,
                        arcname=f"packages/{package_archive.name}",
                    )
            os.replace(staged_archive, final_archive)
            return ContestPackageDownload(
                path=final_archive.resolve(),
                filename=final_archive.name,
                cleanup_root=operation_root,
            )
        except Exception:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
