import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from app.impl.contest.package import _prepare_external_packages
from app.service.contest.package import (
    ContestPackageService,
    ContestPackageSnapshot,
)
from app.service.contest.service import ContestService
from app.service.export.adapters import (
    ContestPackagePlacement,
    PackageAdapterRegistry,
)
from app.service.export.service import CachedExternalPackage
from app.service.problem_package.service import ProblemPackageService


class _ContestService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_generation = 4
        self.roster = [
            {
                "contest_problem_id": 11,
                "idx": "A",
                "problem_id": 101,
                "statement_folder": "A",
                "problem_slug": "alice/alpha",
                "slug_leaf": "alpha",
                "created_at": "2026-08-19T00:00:00+00:00",
            },
            {
                "contest_problem_id": 12,
                "idx": "B",
                "problem_id": 102,
                "statement_folder": "B",
                "problem_slug": "alice/beta",
                "slug_leaf": "beta",
                "created_at": "2026-08-19T00:00:00+00:00",
            },
        ]
        self.download_root_calls = 0

    def contest_context(self, contest_slug: str) -> dict[str, object] | None:
        if contest_slug != "example-contest":
            return None
        return {
            "id": 7,
            "slug": contest_slug,
            "source_generation": self.source_generation,
        }

    def contest_problems(self, _contest_id: int) -> list[dict[str, object]]:
        return self.roster

    def package_download_root(self, _contest_slug: str, operation_id: str) -> Path:
        self.download_root_calls += 1
        root = self.root / operation_id
        root.mkdir(parents=True)
        return root


class _ProblemPackageService:
    def __init__(self) -> None:
        self.statuses = {101: "ready", 102: "ready"}
        self.languages = {
            "np-101": ["english", "chinese"],
            "np-102": ["english", "chinese", "german"],
        }

    def published_readiness_many(
        self, problem_ids: list[int]
    ) -> dict[int, dict[str, object]]:
        return {
            problem_id: {
                "problem_id": problem_id,
                "published_commit": str(problem_id) * 20,
                "published_revision_number": problem_id,
                "native_package_revision_number": problem_id,
                "native_package_id": f"np-{problem_id}",
                "status": self.statuses[problem_id],
                "verified": self.statuses[problem_id] == "ready",
                "missing_reason": "",
            }
            for problem_id in problem_ids
        }

    @staticmethod
    def native_package(native_package_id: str) -> dict[str, object]:
        problem_id = int(native_package_id.removeprefix("np-"))
        return {
            "id": native_package_id,
            "problem_id": problem_id,
            "status": "available",
            "source_commit": str(problem_id) * 20,
            "revision_number": problem_id,
            "archive_sha256": str(problem_id)[-1] * 64,
        }

    def statement_languages(self, native_package_id: str) -> list[str]:
        return self.languages[native_package_id]


class _Adapter:
    def __init__(self, package_format: str) -> None:
        self.format = package_format
        self.placements: list[tuple[str, ContestPackagePlacement]] = []

    def apply_contest_placement(
        self,
        target: Path,
        *,
        canonical_problem_slug: str,
        placement: ContestPackagePlacement,
    ) -> None:
        self.placements.append((canonical_problem_slug, placement))
        (target / "placement.txt").write_text(
            f"{placement.idx}:{placement.ordinal}\n",
            encoding="utf-8",
        )


class _Registry:
    formats = ("domjudge", "icpc-2025-09", "qoj", "nowcoder")

    def __init__(self) -> None:
        self.adapters = {
            package_format: _Adapter(package_format)
            for package_format in self.formats
        }

    @classmethod
    def require_format(cls, package_format: str) -> str:
        if package_format not in cls.formats:
            raise ValueError(f"unsupported package format: {package_format}")
        return package_format

    def require(self, package_format: str) -> _Adapter:
        return self.adapters[self.require_format(package_format)]


class TestContestPackageDownload(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.contest = _ContestService(self.root)
        self.packages = _ProblemPackageService()
        self.registry = _Registry()
        self.service = ContestPackageService(
            cast(ContestService, self.contest),
            cast(PackageAdapterRegistry, self.registry),
            cast(ProblemPackageService, self.packages),
            problem_zip_max_expanded_bytes=4 * 1024 * 1024,
        )

    def _snapshot(self, package_format: str = "domjudge") -> ContestPackageSnapshot:
        return self.service.freeze_download(
            contest_id=7,
            contest_slug="example-contest",
            package_format=package_format,
        )

    def _external_packages(
        self,
        snapshot: ContestPackageSnapshot,
    ) -> dict[int, CachedExternalPackage]:
        result: dict[int, CachedExternalPackage] = {}
        for item in snapshot.items:
            path = self.root / f"external-{item.problem_id}.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "problem.yaml",
                    f"format: {snapshot.package_format}\nname: {item.problem_slug}\n",
                )
                archive.writestr("tests/1", f"input-{item.problem_id}\n")
            result[item.problem_id] = CachedExternalPackage(
                export_id=f"e-{item.problem_id}",
                native_package_id=item.native_package_id,
                package_format=snapshot.package_format,
                filename=path.name,
                path=path,
            )
        return result

    def _statement_pdfs(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for language in ("english", "chinese"):
            path = self.root / f"{language}.pdf"
            path.write_bytes(f"%PDF-{language}\n".encode())
            result[language] = path
        return result

    def test_freeze_requires_ready_packages_and_intersects_languages(self) -> None:
        snapshot = self._snapshot()

        self.assertEqual(snapshot.source_generation, 4)
        self.assertEqual(snapshot.statement_languages, ("english", "chinese"))
        self.assertEqual(
            [item.native_package_id for item in snapshot.items],
            ["np-101", "np-102"],
        )

        self.packages.statuses[102] = "none"
        with self.assertRaisesRegex(ValueError, "Packages are not ready: alice/beta"):
            self._snapshot()
        self.assertEqual(self.contest.download_root_calls, 0)

    def test_validate_snapshot_rejects_contest_changes(self) -> None:
        snapshot = self._snapshot()
        self.contest.source_generation += 1

        with self.assertRaisesRegex(ValueError, "retry download"):
            self.service.validate_snapshot(snapshot)

    def test_freeze_requires_one_common_statement_language(self) -> None:
        self.packages.languages["np-102"] = ["german"]

        with self.assertRaisesRegex(ValueError, "no common statement language"):
            self._snapshot()

    def test_download_assembles_cached_packages_and_common_statements(self) -> None:
        snapshot = self._snapshot()
        external_packages = self._external_packages(snapshot)
        download = self.service.build_download(
            snapshot,
            external_packages=external_packages,
            statement_pdfs=self._statement_pdfs(),
        )

        self.assertEqual(download.filename, "example-contest-domjudge-packages.zip")
        with zipfile.ZipFile(download.path) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "statements.en.pdf",
                    "statements.zh.pdf",
                    "packages/A-alice-alpha.zip",
                    "packages/B-alice-beta.zip",
                ],
            )
            self.assertEqual(archive.read("statements.en.pdf"), b"%PDF-english\n")
            first_package = io.BytesIO(
                archive.read("packages/A-alice-alpha.zip")
            )
            with zipfile.ZipFile(first_package) as package:
                self.assertEqual(
                    package.read("problem.yaml"),
                    b"format: domjudge\nname: alice/alpha\n",
                )
                self.assertEqual(package.read("tests/1"), b"input-101\n")
                self.assertEqual(package.read("placement.txt"), b"A:1\n")

        adapter = self.registry.adapters["domjudge"]
        self.assertEqual(
            adapter.placements,
            [
                ("alice/alpha", ContestPackagePlacement(idx="A", ordinal=1)),
                ("alice/beta", ContestPackagePlacement(idx="B", ordinal=2)),
            ],
        )
        with zipfile.ZipFile(external_packages[101].path) as cached_package:
            self.assertNotIn("placement.txt", cached_package.namelist())
        cleanup_root = download.cleanup_root
        download.close()
        self.assertFalse(cleanup_root.exists())

    def test_download_accepts_each_registered_adapter(self) -> None:
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                download = self.service.build_download(
                    snapshot,
                    external_packages=self._external_packages(snapshot),
                    statement_pdfs=self._statement_pdfs(),
                )
                self.assertTrue(download.path.is_file())
                download.close()

    def test_download_rejects_incomplete_inputs(self) -> None:
        snapshot = self._snapshot()
        external_packages = self._external_packages(snapshot)
        external_packages.pop(102)
        with self.assertRaisesRegex(ValueError, "external package set is incomplete"):
            self.service.build_download(
                snapshot,
                external_packages=external_packages,
                statement_pdfs=self._statement_pdfs(),
            )

    def test_download_rejects_an_invalid_cached_external_archive(self) -> None:
        snapshot = self._snapshot()
        external_packages = self._external_packages(snapshot)
        external_packages[101].path.write_bytes(b"not a zip")

        with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
            self.service.build_download(
                snapshot,
                external_packages=external_packages,
                statement_pdfs=self._statement_pdfs(),
            )

    def test_download_reports_the_expanded_limit_for_a_cached_archive(self) -> None:
        snapshot = self._snapshot()
        external_packages = self._external_packages(snapshot)
        with zipfile.ZipFile(external_packages[101].path, "w") as archive:
            archive.writestr("data/secret/020.ans", b"x" * (4 * 1024 * 1024 + 1))

        with self.assertRaises(ValueError) as raised:
            self.service.build_download(
                snapshot,
                external_packages=external_packages,
                statement_pdfs=self._statement_pdfs(),
            )

        self.assertEqual(
            str(raised.exception),
            "cached external package is invalid: external-101.zip: "
            "expanded zip payload is too large at data/secret/020.ans; "
            "increase PROBLEM_ZIP_MAX_EXPANDED_BYTES (currently 4194304 bytes)",
        )

    def test_freeze_rejects_unregistered_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported package format: custom"):
            self._snapshot("custom")
        self.assertEqual(self.contest.download_root_calls, 0)

    def test_prepare_submits_every_missing_export_before_waiting(self) -> None:
        snapshot = self._snapshot()
        ready: dict[int, CachedExternalPackage] = {}
        submitted: list[int] = []
        test_root = self.root

        def cached_external_package(
            *,
            problem_id: int,
            native_package_id: str,
            package_format: str,
        ) -> CachedExternalPackage | None:
            del native_package_id, package_format
            return ready.get(problem_id)

        fake_runtime = SimpleNamespace(
            export_service=SimpleNamespace(
                cached_external_package=cached_external_package,
            ),
            config_values=SimpleNamespace(integer=lambda _key: 4096),
        )

        class Future:
            def __init__(self, item_problem_id: int) -> None:
                self.problem_id = item_problem_id

            def join(self) -> None:
                self.assert_all_submitted()
                item = next(
                    row
                    for row in snapshot.items
                    if row.problem_id == self.problem_id
                )
                path = test_root / f"prepared-{self.problem_id}.zip"
                path.write_bytes(b"external")
                ready[self.problem_id] = CachedExternalPackage(
                    export_id=f"e-{self.problem_id}",
                    native_package_id=item.native_package_id,
                    package_format=snapshot.package_format,
                    filename=path.name,
                    path=path,
                )

            @staticmethod
            def exception() -> None:
                return None

            @staticmethod
            def assert_all_submitted() -> None:
                if len(submitted) != 2:
                    raise AssertionError("waiting started before all jobs were submitted")

        def start_job(*_args: object, problem_id: int, **_kwargs: object):
            submitted.append(problem_id)
            return (f"job-{problem_id}", Future(problem_id))

        with (
            patch("app.impl.contest.package.runtime", return_value=fake_runtime),
            patch(
                "app.impl.contest.package.start_ready_external_export_job",
                side_effect=start_job,
            ) as start,
        ):
            result = _prepare_external_packages(snapshot, actor_user_id=9)

        self.assertEqual(submitted, [101, 102])
        self.assertEqual(set(result), {101, 102})
        self.assertEqual(start.call_count, 2)

    def test_prepare_reuses_complete_external_cache(self) -> None:
        snapshot = self._snapshot()
        cached = self._external_packages(snapshot)
        fake_runtime = SimpleNamespace(
            export_service=SimpleNamespace(
                cached_external_package=Mock(
                    side_effect=lambda *, problem_id, **_kwargs: cached[problem_id]
                )
            ),
            config_values=SimpleNamespace(integer=lambda _key: 4096),
        )
        with (
            patch("app.impl.contest.package.runtime", return_value=fake_runtime),
            patch(
                "app.impl.contest.package.start_ready_external_export_job"
            ) as start,
        ):
            result = _prepare_external_packages(snapshot, actor_user_id=9)

        self.assertEqual(result, cached)
        start.assert_not_called()

    def test_prepare_reports_problem_identity_format_and_worker_error(self) -> None:
        snapshot = self._snapshot()
        fake_runtime = SimpleNamespace(
            export_service=SimpleNamespace(
                cached_external_package=Mock(return_value=None),
            ),
            config_values=SimpleNamespace(integer=lambda _key: 4096),
        )

        class FailedFuture:
            @staticmethod
            def join() -> None:
                return None

            @staticmethod
            def exception() -> ValueError:
                return ValueError("adapter failed")

        with (
            patch("app.impl.contest.package.runtime", return_value=fake_runtime),
            patch(
                "app.impl.contest.package.start_ready_external_export_job",
                side_effect=lambda *_args, **_kwargs: (
                    "failed-job",
                    FailedFuture(),
                ),
            ),
            self.assertRaisesRegex(
                ValueError,
                r"A alice/alpha \[domjudge\]: adapter failed",
            ),
        ):
            _prepare_external_packages(snapshot, actor_user_id=9)


if __name__ == "__main__":
    unittest.main()
