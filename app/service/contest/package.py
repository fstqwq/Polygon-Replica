import json
import shutil
from pathlib import Path

from app.service.contest.metadata import materialize_contest_problem_package
from app.service.contest.naming import problem_slug_file_token
from app.service.contest.service import ContestService
from app.service.export.service import ExportService


class ContestPackageService:
    """Convert frozen Native inputs and publish one atomic Contest bundle."""

    def __init__(
        self,
        contest_service: ContestService,
        export_service: ExportService,
    ) -> None:
        self._contest = contest_service
        self._exports = export_service

    @staticmethod
    def _zip_bundle(job_root: Path, bundle_name: str, source_dir: Path) -> Path:
        safe_name = Path(bundle_name.strip() or "contest-bundle").stem
        output = shutil.make_archive(
            str(job_root / (safe_name or "contest-bundle")),
            "zip",
            root_dir=source_dir,
            base_dir=".",
        )
        return Path(output).resolve()

    def build_bundle(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        job_id: str,
    ) -> dict[str, object]:
        job_root = self._contest.job_root(contest_slug, job_id)
        packages_dir = job_root / "packages"
        packages_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, object]] = []
        for entry in self._contest.build_items(job_id):
            label = str(entry["label"])
            problem_slug = str(entry["problem_slug"])
            item: dict[str, object] = {
                "idx": label,
                "problem_id": int(entry["problem_id"]),
                "problem_slug": problem_slug,
                "status": "failed",
                "source_commit": str(entry["source_commit"]),
                "materialization_id": str(entry["materialization_id"]),
                "package_file": "",
                "error": "",
            }
            try:
                export_id, export_path = self._exports.create_export(
                    problem_slug,
                    "icpc",
                    materialization_id=str(entry["materialization_id"]),
                    expected_archive_sha256=str(entry["archive_sha256"]),
                )
                item["export_id"] = export_id
                canonical = Path(export_path).resolve()
                if canonical.is_symlink() or not canonical.is_file():
                    raise RuntimeError("package file missing")
                token = problem_slug_file_token(problem_slug)
                filename = f"{label}-{token}.zip" if label else f"{token}.zip"
                target = (packages_dir / filename).resolve()
                materialize_contest_problem_package(
                    canonical,
                    target,
                    short_name=label,
                    staging_parent=job_root / ".package-staging",
                )
                item["package_file"] = f"packages/{filename}"
                item["status"] = "success"
            except Exception as exc:  # Preserve one result row per frozen problem.
                item["error"] = str(exc)
            results.append(item)
        successes = [row for row in results if row["status"] == "success"]
        failed = [row for row in results if row["status"] != "success"]
        bundle_root = job_root / "bundle-package"
        shutil.rmtree(bundle_root, ignore_errors=True)
        bundle_root.mkdir(parents=True, exist_ok=True)
        if packages_dir.exists() and any(packages_dir.iterdir()):
            shutil.copytree(
                packages_dir,
                bundle_root / "packages",
                dirs_exist_ok=True,
            )
        summary: dict[str, object] = {
            "job_type": "package",
            "contest_slug": contest_slug,
            "results": results,
            "totals": {
                "total": len(results),
                "success": len(successes),
                "failed": len(failed),
            },
        }
        errors = [str(row["error"]) for row in failed]
        if len(failed) == len(results) and len(failed) > 1:
            if len(set(errors)) == 1 and errors[0]:
                summary["common_error"] = errors[0]
        (bundle_root / "manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact_id = ""
        filename = ""
        if successes and not failed:
            archive = self._zip_bundle(
                job_root,
                f"{contest_slug}-packages-{job_id}",
                bundle_root,
            )
            filename = archive.name
            artifact_id = self._contest.record_artifact(
                contest_id=contest_id,
                job_id=job_id,
                artifact_type="package-bundle",
                filename=filename,
                artifact_path=archive,
            )
        summary["artifact_id"] = artifact_id
        summary["filename"] = filename
        return summary
