import json
import os
import shutil
from pathlib import Path

from app.service.contest.naming import problem_slug_file_token
from app.service.contest.service import ContestService
from app.service.export.adapters import PackageAdapterRegistry, PackageFormat
from app.service.problem_package.service import NativePackageReader


class ContestPackageService:
    """Project frozen Native Packages into one atomic Contest bundle."""

    def __init__(
        self,
        contest_service: ContestService,
        package_adapters: PackageAdapterRegistry,
    ) -> None:
        self._contest = contest_service
        self._package_adapters = package_adapters

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

    def build_bundle(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        job_id: str,
        package_format: PackageFormat,
        readers: dict[str, NativePackageReader],
    ) -> dict[str, object]:
        adapter = self._package_adapters.require(package_format)
        job_root = self._contest.job_root(contest_slug, job_id)
        output_token = adapter.format
        staging = job_root / f".{output_token}-bundle-staging"
        shutil.rmtree(staging, ignore_errors=True)
        package_roots = staging / "package-roots"
        packages_dir = staging / "bundle" / "packages"
        package_roots.mkdir(parents=True, exist_ok=False)
        packages_dir.mkdir(parents=True, exist_ok=False)
        final_archive: Path | None = None

        try:
            results: list[dict[str, object]] = []
            manifest_items: list[dict[str, object]] = []
            for entry in self._contest.build_items(job_id):
                idx = str(entry["idx"])
                problem_slug = str(entry["problem_slug"])
                materialization_id = str(entry["materialization_id"])
                item: dict[str, object] = {
                    "idx": idx,
                    "problem_id": int(entry["problem_id"]),
                    "problem_slug": problem_slug,
                    "status": "failed",
                    "source_commit": str(entry["source_commit"]),
                    "revision_number": int(entry["revision_number"]),
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
                        short_name=idx if adapter.accepts_short_name else None,
                    )
                    token = problem_slug_file_token(problem_slug)
                    filename = f"{idx}-{token}.zip" if idx else f"{token}.zip"
                    target = self._zip_directory(packages_dir / filename, package_root)
                    item["package_file"] = f"packages/{target.name}"
                    item["status"] = "success"
                    manifest_items.append(
                        {
                            "idx": idx,
                            "problem": problem_slug,
                            "revision": int(entry["revision_number"]),
                            "source_commit": str(entry["source_commit"]),
                            "native_package_id": materialization_id,
                            "archive_sha256": str(entry["archive_sha256"]),
                            "package": f"packages/{target.name}",
                        }
                    )
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

            bundle_root = staging / "bundle"
            (bundle_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "contest": contest_slug,
                        "format": package_format,
                        "problems": manifest_items,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            filename = f"{contest_slug}-{output_token}-packages-{job_id}.zip"
            final_archive = job_root / filename
            if final_archive.is_symlink():
                raise RuntimeError("contest bundle target must not be a symbolic link")
            final_archive.unlink(missing_ok=True)
            staged_archive = self._zip_directory(
                staging / filename,
                bundle_root,
            )
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
