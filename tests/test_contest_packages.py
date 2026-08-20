import io
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, cast

from app.service.contest.package import ContestPackageService
from app.service.contest.service import ContestService
from app.service.export.adapters import PackageAdapterRegistry
from app.service.problem_package.service import (
    NativePackageReader,
    ProblemPackageService,
)


class _ContestService:
    def __init__(self, root: Path) -> None:
        self.root = root
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
        self.opened: list[tuple[str, str]] = []

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
            "status": "available",
            "source_commit": str(problem_id) * 20,
            "revision_number": problem_id,
            "archive_sha256": str(problem_id)[-1] * 64,
        }

    @contextmanager
    def open_reader(
        self,
        native_package_id: str,
        *,
        expected_archive_sha256: str,
    ) -> Iterator[NativePackageReader]:
        self.opened.append((native_package_id, expected_archive_sha256))
        yield cast(
            NativePackageReader,
            SimpleNamespace(root=Path("."), manifest={}),
        )


class _Adapter:
    format = "domjudge"
    accepts_short_name = True

    def build(
        self,
        _reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        short_name: str | None = None,
        **_kwargs: object,
    ) -> str:
        (target / "problem.yaml").write_text(
            f"name: {canonical_problem_slug}\nidx: {short_name}\n",
            encoding="utf-8",
        )
        return ""


class _Registry:
    @staticmethod
    def require_format(package_format: str) -> str:
        if package_format != "domjudge":
            raise ValueError(f"unsupported package format: {package_format}")
        return package_format

    @staticmethod
    def require(package_format: str) -> _Adapter:
        if package_format != "domjudge":
            raise ValueError(f"unsupported package format: {package_format}")
        return _Adapter()


class TestContestPackageDownload(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.contest = _ContestService(Path(self.temp_dir.name))
        self.packages = _ProblemPackageService()
        self.service = ContestPackageService(
            cast(ContestService, self.contest),
            cast(PackageAdapterRegistry, _Registry()),
            cast(ProblemPackageService, self.packages),
        )

    def test_download_uses_current_packages_and_cleans_transient_bundle(self) -> None:
        download = self.service.build_download(
            contest_id=7,
            contest_slug="example-contest",
            package_format="domjudge",
        )

        self.assertEqual(download.filename, "example-contest-domjudge-packages.zip")
        self.assertTrue(download.path.is_file())
        with zipfile.ZipFile(download.path) as archive:
            package_entries = archive.infolist()
            self.assertEqual(
                [entry.filename for entry in package_entries],
                [
                    "packages/A-alice-alpha.zip",
                    "packages/B-alice-beta.zip",
                ],
            )
            self.assertTrue(
                all(
                    entry.compress_type == zipfile.ZIP_STORED
                    for entry in package_entries
                )
            )
            with zipfile.ZipFile(io.BytesIO(archive.read(package_entries[0]))) as package:
                self.assertEqual(
                    package.read("problem.yaml"),
                    b"name: alice/alpha\nidx: A\n",
                )
        self.assertEqual(
            self.packages.opened,
            [("np-101", "1" * 64), ("np-102", "2" * 64)],
        )

        cleanup_root = download.cleanup_root
        download.close()
        self.assertFalse(cleanup_root.exists())

    def test_download_rejects_contest_before_creating_partial_bundle(self) -> None:
        self.packages.statuses[102] = "none"

        with self.assertRaisesRegex(ValueError, "Packages are not ready: alice/beta"):
            self.service.build_download(
                contest_id=7,
                contest_slug="example-contest",
                package_format="domjudge",
            )

        self.assertEqual(self.contest.download_root_calls, 0)
        self.assertEqual(self.packages.opened, [])

    def test_download_rejects_non_contest_adapter(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported package format: qoj"):
            self.service.build_download(
                contest_id=7,
                contest_slug="example-contest",
                package_format="qoj",
            )

        self.assertEqual(self.contest.download_root_calls, 0)


if __name__ == "__main__":
    unittest.main()
