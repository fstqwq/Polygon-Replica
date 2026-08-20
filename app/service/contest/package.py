import os
import shutil
import uuid
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from app.service.contest.naming import problem_slug_file_token
from app.service.contest.service import ContestService
from app.service.export.adapters import (
    ContestPackagePlacement,
    PackageAdapterRegistry,
    PackageFormat,
)
from app.service.problem_package.service import (
    NativePackageReader,
    ProblemPackageService,
)


@dataclass(frozen=True)
class ContestPackageDownload:
    path: Path
    filename: str
    cleanup_root: Path

    def close(self) -> None:
        shutil.rmtree(self.cleanup_root, ignore_errors=True)


class ContestPackageService:
    """Project frozen Native Packages into one atomic Contest bundle."""

    def __init__(
        self,
        contest_service: ContestService,
        package_adapters: PackageAdapterRegistry,
        problem_package_service: ProblemPackageService,
    ) -> None:
        self._contest = contest_service
        self._package_adapters = package_adapters
        self._problem_packages = problem_package_service

    @staticmethod
    def _zip_directory(destination: Path, source_dir: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive = shutil.make_archive(
            str(destination.with_suffix("")),
            "zip",
            root_dir=source_dir,
            base_dir=".",
        )
        return Path(archive).resolve()

    @staticmethod
    def _bundle_packages(destination: Path, packages: list[Path]) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            destination,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for package in packages:
                archive.write(package, arcname=f"packages/{package.name}")
        return destination.resolve()

    def _build_bundle(
        self,
        *,
        contest_slug: str,
        operation_root: Path,
        items: list[dict[str, object]],
        package_format: PackageFormat,
        readers: dict[str, NativePackageReader],
    ) -> dict[str, object]:
        adapter = self._package_adapters.require(package_format)
        output_token = adapter.format
        staging = operation_root / f".{output_token}-bundle-staging"
        shutil.rmtree(staging, ignore_errors=True)
        package_roots = staging / "package-roots"
        packages_dir = staging / "bundle" / "packages"
        package_roots.mkdir(parents=True, exist_ok=False)
        packages_dir.mkdir(parents=True, exist_ok=False)
        final_archive: Path | None = None

        try:
            results: list[dict[str, object]] = []
            package_archives: list[Path] = []
            for entry in items:
                idx = str(entry["idx"])
                problem_slug = str(entry["problem_slug"])
                materialization_id = str(entry["materialization_id"])
                item: dict[str, object] = {
                    "idx": idx,
                    "problem_id": int(str(entry["problem_id"])),
                    "problem_slug": problem_slug,
                    "status": "failed",
                    "source_commit": str(entry["source_commit"]),
                    "revision_number": int(str(entry["revision_number"])),
                    "native_package_id": materialization_id,
                    "archive_sha256": str(entry["archive_sha256"]),
                    "package_file": "",
                    "warning": "",
                    "error": "",
                }
                try:
                    reader = readers[materialization_id]
                    package_root = package_roots / str(entry["contest_problem_id"])
                    package_root.mkdir(parents=True, exist_ok=False)
                    item["warning"] = adapter.build(
                        reader,
                        target=package_root,
                        canonical_problem_slug=problem_slug,
                        placement=ContestPackagePlacement(
                            idx=idx,
                            ordinal=int(str(entry["ordinal"])),
                        ),
                    )
                    token = problem_slug_file_token(problem_slug)
                    filename = f"{idx}-{token}.zip" if idx else f"{token}.zip"
                    target = self._zip_directory(packages_dir / filename, package_root)
                    item["package_file"] = f"packages/{target.name}"
                    item["status"] = "success"
                    package_archives.append(target)
                except Exception as exc:
                    item["error"] = str(exc)
                results.append(item)

            successes = [row for row in results if row["status"] == "success"]
            failed = [row for row in results if row["status"] != "success"]
            summary: dict[str, object] = {
                "job_type": "package",
                "format": package_format,
                "contest_slug": contest_slug,
                "results": results,
                "totals": {
                    "total": len(results),
                    "success": len(successes),
                    "failed": len(failed),
                },
                "artifact_id": "",
                "filename": "",
            }
            warnings = [
                {
                    "problem": str(row["problem_slug"]),
                    "message": str(row["warning"]),
                }
                for row in results
                if row["warning"]
            ]
            if warnings:
                summary["warnings"] = warnings
            if failed:
                errors = [str(row["error"]) for row in failed]
                summary["error"] = errors[0] or "problem package build failed"
                if len(failed) == len(results) and len(failed) > 1:
                    if len(set(errors)) == 1 and errors[0]:
                        summary["common_error"] = errors[0]
                return summary

            filename = f"{contest_slug}-{output_token}-packages.zip"
            final_archive = operation_root / filename
            if final_archive.is_symlink():
                raise RuntimeError("contest bundle target must not be a symbolic link")
            final_archive.unlink(missing_ok=True)
            staged_archive = self._bundle_packages(staging / filename, package_archives)
            archive = final_archive
            os.replace(staged_archive, archive)
            summary["_artifact_path"] = str(archive)
            summary["_artifact_type"] = f"{output_token}-bundle"
            summary["_artifact_filename"] = archive.name
            summary["filename"] = archive.name
            final_archive = None
            return summary
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if final_archive is not None:
                final_archive.unlink(missing_ok=True)

    def build_download(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        package_format: str,
    ) -> ContestPackageDownload:
        safe_format = self._package_adapters.require_format(package_format)

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

        items: list[dict[str, object]] = []
        for ordinal, row in enumerate(roster, start=1):
            package = readiness[int(row["problem_id"])]
            native_package = self._problem_packages.native_package(
                package["native_package_id"]
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
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "ordinal": ordinal,
                    "idx": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": str(row["problem_slug"]),
                    "statement_folder": str(row["statement_folder"]),
                    "source_commit": native_package["source_commit"],
                    "revision_number": native_package["revision_number"],
                    "materialization_id": native_package["id"],
                    "archive_sha256": native_package["archive_sha256"],
                }
            )

        operation_id = f"download-{uuid.uuid4().hex[:12]}"
        operation_root = self._contest.package_download_root(
            contest_slug,
            operation_id,
        )
        try:
            with ExitStack() as readers_stack:
                readers = {
                    str(item["materialization_id"]): readers_stack.enter_context(
                        self._problem_packages.open_reader(
                            str(item["materialization_id"]),
                            expected_archive_sha256=str(item["archive_sha256"]),
                        )
                    )
                    for item in items
                }
                summary = self._build_bundle(
                    contest_slug=contest_slug,
                    operation_root=operation_root,
                    items=items,
                    package_format=safe_format,
                    readers=readers,
                )
            error = summary.get("error")
            archive_value = summary.get("_artifact_path")
            filename_value = summary.get("_artifact_filename")
            if error:
                raise ValueError(str(error))
            if not isinstance(archive_value, str) or not archive_value:
                raise RuntimeError("Contest package download was not produced")
            if not isinstance(filename_value, str) or not filename_value:
                raise RuntimeError("Contest package filename is missing")
            archive_path = Path(archive_value)
            if not archive_path.is_file() or archive_path.is_symlink():
                raise RuntimeError("Contest package download is unavailable")
            return ContestPackageDownload(
                path=archive_path,
                filename=filename_value,
                cleanup_root=operation_root,
            )
        except Exception:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise
