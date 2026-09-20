import io
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from app.config import ConfigValues
from app.impl.contest.package import _prepare_external_packages
from app.service.contest.package import (
    ContestPackageService,
    ContestPackageSnapshot,
)
from app.service.contest.service import ContestService
from app.service.export.adapters import PackageAdapterRegistry
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


class TestContestPackageDownload(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.contest = _ContestService(self.root)
        self.packages = _ProblemPackageService()
        self.registry = PackageAdapterRegistry(
            ConfigValues({}, normalizer=lambda raw: raw),
            Mock(),
        )
        self.service = ContestPackageService(
            cast(ContestService, self.contest),
            self.registry,
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
                if snapshot.package_format == "domjudge":
                    archive.writestr(
                        "domjudge-problem.ini",
                        f"externalid = {item.problem_slug}\n"
                        "short-name = standalone\n"
                        "color = #123456\n",
                    )
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
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                cached_payloads = {
                    problem_id: cached.path.read_bytes()
                    for problem_id, cached in external_packages.items()
                }
                download = self.service.build_download(
                    snapshot,
                    external_packages=external_packages,
                    statement_pdfs=self._statement_pdfs(),
                )
                self.assertEqual(
                    download.filename,
                    f"example-contest-{package_format}-packages.zip",
                )
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
                    for item in snapshot.items:
                        token = item.problem_slug.replace("/", "-")
                        payload = archive.read(f"packages/{item.idx}-{token}.zip")
                        if package_format != "domjudge":
                            self.assertEqual(
                                payload,
                                cached_payloads[item.problem_id],
                            )
                        with zipfile.ZipFile(io.BytesIO(payload)) as package:
                            self.assertEqual(
                                package.read("tests/1"),
                                f"input-{item.problem_id}\n".encode(),
                            )
                            self.assertEqual(
                                package.read("problem.yaml"),
                                f"format: {package_format}\nname: {item.problem_slug}\n".encode(),
                            )
                            if package_format == "domjudge":
                                color = ("#e6194b", "#4363d8")[item.ordinal - 1]
                                self.assertEqual(
                                    package.read("domjudge-problem.ini").decode(),
                                    f"externalid = {item.problem_slug}\n"
                                    f"short-name = {item.idx}\n"
                                    f"color = {color}\n",
                                )
                for problem_id, cached in external_packages.items():
                    self.assertEqual(cached.path.read_bytes(), cached_payloads[problem_id])
                download.close()
                self.assertFalse(download.cleanup_root.exists())

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
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                external_packages[101].path.write_bytes(b"not a zip")

                with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                    self.service.build_download(
                        snapshot,
                        external_packages=external_packages,
                        statement_pdfs=self._statement_pdfs(),
                    )

    def test_download_rejects_corrupt_members_and_unsafe_paths(self) -> None:
        for package_format in self.registry.formats:
            for invalid_kind in ("crc", "path", "conflict"):
                with self.subTest(package_format=package_format, invalid_kind=invalid_kind):
                    snapshot = self._snapshot(package_format)
                    external_packages = self._external_packages(snapshot)
                    path = external_packages[101].path
                    if invalid_kind == "crc":
                        path.write_bytes(
                            path.read_bytes().replace(b"input-101\n", b"input-109\n", 1)
                        )
                    else:
                        with zipfile.ZipFile(path, "w") as archive:
                            if invalid_kind == "path":
                                archive.writestr("../outside", b"payload")
                            else:
                                archive.writestr("tests", b"payload")
                                archive.writestr("tests/1", b"payload")
                    with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                        self.service.build_download(
                            snapshot,
                            external_packages=external_packages,
                            statement_pdfs=self._statement_pdfs(),
                        )

    def test_unchanged_packages_validate_directory_entries(self) -> None:
        for package_format in self.registry.formats:
            if package_format == "domjudge":
                continue
            for invalid_kind in ("symlink", "crc", "expanded"):
                with self.subTest(package_format=package_format, invalid_kind=invalid_kind):
                    snapshot = self._snapshot(package_format)
                    external_packages = self._external_packages(snapshot)
                    path = external_packages[101].path
                    directory = zipfile.ZipInfo("payload/")
                    directory.external_attr = (
                        stat.S_IFLNK if invalid_kind == "symlink" else stat.S_IFDIR
                    ) << 16
                    content = (
                        b"x" * (4 * 1024 * 1024 + 1)
                        if invalid_kind == "expanded"
                        else b"directory payload"
                    )
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(directory, content)
                    if invalid_kind == "crc":
                        path.write_bytes(
                            path.read_bytes().replace(content, b"Directory payload", 1)
                        )
                    with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                        self.service.build_download(
                            snapshot,
                            external_packages=external_packages,
                            statement_pdfs=self._statement_pdfs(),
                        )

    def test_download_reports_the_expanded_limit_for_a_cached_archive(self) -> None:
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                with zipfile.ZipFile(external_packages[101].path, "w") as archive:
                    archive.writestr("data/secret/020.ans", b"x" * (4 * 1024 * 1024 + 1))

                with self.assertRaises(ValueError) as raised:
                    self.service.build_download(
                        snapshot,
                        external_packages=external_packages,
                        statement_pdfs=self._statement_pdfs(),
                    )

                filename = (
                    "external-101.zip"
                    if package_format == "domjudge"
                    else "A-alice-alpha.zip"
                )
                self.assertEqual(
                    str(raised.exception),
                    f"cached external package is invalid: {filename}: "
                    "expanded zip payload is too large at data/secret/020.ans; "
                    "increase PROBLEM_ZIP_MAX_EXPANDED_BYTES (currently 4194304 bytes)",
                )

    def test_freeze_rejects_unregistered_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported package format: custom"):
            self._snapshot("custom")
        self.assertEqual(self.contest.download_root_calls, 0)

    def test_prepare_submits_missing_exports_together_and_reuses_completed_cache(self) -> None:
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
            ),
        ):
            result = _prepare_external_packages(snapshot, actor_user_id=9)
            reused = _prepare_external_packages(snapshot, actor_user_id=9)

        self.assertEqual(submitted, [101, 102])
        self.assertEqual(set(result), {101, 102})
        self.assertEqual(reused, result)

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
