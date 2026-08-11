from __future__ import annotations

import io
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile

from app.service.importing.archive import (
    BudgetedZipFile,
    ExpansionBudget,
    MetadataBudget,
    PROBLEM_ZIP_MAX_ENTRIES,
    contest_archive_policy,
)
from app.service.importing.native import NativePackageImportService
from app.service.importing.upload import spool_fileobj
from app.service.importing.polygon import polygon_solution_expected_from_tag
from tests.archive_support import archive_view_from_bytes


class TestPolygonPackageRules(unittest.TestCase):
    def test_oversized_upload_spool_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="archive-spool-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "package is too large"):
                with spool_fileobj(
                    io.BytesIO(b"12345"),
                    root=root,
                    max_bytes=4,
                    label="package",
                ):
                    pass
            self.assertEqual(list(root.iterdir()), [])

    def test_contest_archive_budget_is_derived_from_problem_budget(self) -> None:
        policy = contest_archive_policy(26, 256 * 1024 * 1024)
        self.assertEqual(policy.max_entries, 26 * PROBLEM_ZIP_MAX_ENTRIES)
        self.assertEqual(policy.max_expanded_bytes, 26 * 256 * 1024 * 1024)

    def test_solution_tags_map_to_canonical_expected_behaviors(self) -> None:
        expectations = {
            "main": "accepted",
            "accepted": "accepted",
            "wrong-answer": "wrong_answer",
            "presentation-error": "wrong_answer",
            "time-limit-exceeded": "time_limit_exceeded",
            "time-limit-exceeded-or-accepted": "tle_or_correct",
            "time-limit-exceeded-or-memory-limit-exceeded": "tle_or_re",
            "memory-limit-exceeded": "run_time_error",
            "rejected": "rejected",
            "failed": "rejected",
            "do-not-run": "unknown",
        }

        for tag, expected in expectations.items():
            with self.subTest(tag=tag):
                self.assertEqual(polygon_solution_expected_from_tag(tag), expected)

    @staticmethod
    def _zip(entries: list[tuple[str, bytes]]) -> bytes:
        payload = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(
                payload,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for name, content in entries:
                    archive.writestr(name, content)
        return payload.getvalue()

    @staticmethod
    def _with_zip64_end_records(payload: bytes) -> bytes:
        raw = bytearray(payload)
        eocd_offset = len(raw) - struct.calcsize("<4s4H2LH")
        eocd = struct.unpack_from("<4s4H2LH", raw, eocd_offset)
        if eocd[0] != b"PK\x05\x06" or eocd[-1] != 0:
            raise AssertionError("test ZIP must have a comment-free EOCD")
        entry_count = int(eocd[4])
        central_size = int(eocd[5])
        central_offset = int(eocd[6])
        zip64_record = struct.pack(
            "<4sQ2H2L4Q",
            b"PK\x06\x06",
            44,
            45,
            45,
            0,
            0,
            entry_count,
            entry_count,
            central_size,
            central_offset,
        )
        locator = struct.pack(
            "<4sLQL",
            b"PK\x06\x07",
            0,
            eocd_offset,
            1,
        )
        classic = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        return bytes(raw[:eocd_offset] + zip64_record + locator + classic)

    def test_archive_entry_limit_counts_directories_and_ignored_members(self) -> None:
        entries = [("test_data/", b"")]
        entries.extend(
            (f"test_data/ignored-{index:04d}", b"")
            for index in range(4095)
        )
        payload = self._zip(entries)
        with archive_view_from_bytes(payload, max_entries=4096) as archive:
            self.assertEqual(len(archive.entries), 4096)

        oversized = self._zip([*entries, ("ignored-extra", b"")])
        with self.assertRaisesRegex(ValueError, "more than 4096 entries"):
            with archive_view_from_bytes(oversized, max_entries=4096):
                pass

    def test_archive_preflight_rejects_duplicate_traversal_and_multidisk(self) -> None:
        duplicate = self._zip([("a", b"1"), ("a", b"2")])
        with self.assertRaisesRegex(ValueError, "duplicate zip path"):
            with archive_view_from_bytes(duplicate):
                pass

        normalized_duplicate = self._zip(
            [("folder/./entry", b"1"), ("folder/entry", b"2")]
        )
        with self.assertRaisesRegex(ValueError, "duplicate zip path"):
            with archive_view_from_bytes(normalized_duplicate):
                pass

        traversal = self._zip([("../outside", b"x")])
        with self.assertRaisesRegex(ValueError, "invalid zip path"):
            with archive_view_from_bytes(traversal):
                pass

        multidisk = bytearray(self._zip([("a", b"1")]))
        struct.pack_into("<H", multidisk, len(multidisk) - 18, 1)
        with self.assertRaisesRegex(ValueError, "multi-disk"):
            with archive_view_from_bytes(bytes(multidisk)):
                pass

        out_of_bounds = bytearray(self._zip([("a", b"1")]))
        struct.pack_into("<L", out_of_bounds, len(out_of_bounds) - 6, 0xFFFFFF00)
        with self.assertRaisesRegex(ValueError, "central directory is out of bounds"):
            with archive_view_from_bytes(bytes(out_of_bounds)):
                pass

        with self.assertRaisesRegex(ValueError, "archive end record not found"):
            with archive_view_from_bytes(b"x" * 22):
                pass

    def test_content_level_entry_checks_run_only_when_selected(self) -> None:
        for name, mode, error in (
            ("link", stat.S_IFLNK | 0o777, "zip symlink is not allowed"),
            ("fifo", stat.S_IFIFO | 0o600, "zip special file is not allowed"),
        ):
            with self.subTest(name=name):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w") as archive:
                    archive.writestr(info, b"payload")
                with archive_view_from_bytes(payload.getvalue()) as archive:
                    selected = archive.entries[name]
                    with self.assertRaisesRegex(ValueError, error):
                        archive.zip_file.open(selected)

        encrypted = bytearray(self._zip([("encrypted", b"payload")]))
        local_offset = encrypted.find(b"PK\x03\x04")
        central_offset = encrypted.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        local_flags = struct.unpack_from("<H", encrypted, local_offset + 6)[0]
        central_flags = struct.unpack_from("<H", encrypted, central_offset + 8)[0]
        struct.pack_into("<H", encrypted, local_offset + 6, local_flags | 0x1)
        struct.pack_into("<H", encrypted, central_offset + 8, central_flags | 0x1)
        with archive_view_from_bytes(bytes(encrypted)) as archive:
            with self.assertRaisesRegex(
                ValueError,
                "encrypted zip entry is not supported",
            ):
                archive.zip_file.open(archive.entries["encrypted"])

    def test_archive_rejects_conflicting_parent_file(self) -> None:
        payload = self._zip([("folder", b"file"), ("folder/child", b"child")])
        with self.assertRaisesRegex(ValueError, "conflicting zip paths"):
            with archive_view_from_bytes(payload):
                pass

    def test_archive_allows_single_disk_zip64_within_budgets(self) -> None:
        payload = self._with_zip64_end_records(
            self._zip([("payload", b"zip64\n")])
        )
        with archive_view_from_bytes(payload) as archive:
            with archive.zip_file.open(archive.entries["payload"]) as source:
                self.assertEqual(source.read(), b"zip64\n")

    def test_metadata_limit_has_exact_boundary(self) -> None:
        payload = self._zip([("metadata", b"12345")])
        with archive_view_from_bytes(
            payload,
            max_metadata_bytes=4,
        ) as archive:
            info = archive.entries["metadata"]
            with self.assertRaisesRegex(ValueError, "metadata is too large"):
                archive.read_metadata(info)

        exact = self._zip([("metadata", b"1234")])
        with archive_view_from_bytes(
            exact,
            max_metadata_bytes=4,
        ) as archive:
            self.assertEqual(
                archive.read_metadata(archive.entries["metadata"]),
                b"1234",
            )

    def test_expansion_budget_checks_declared_and_actual_bytes(self) -> None:
        payload = self._zip([("selected", b"12345")])
        with archive_view_from_bytes(
            payload,
            max_expanded_bytes=4,
        ) as archive:
            with self.assertRaisesRegex(ValueError, "expanded zip payload is too large"):
                archive.zip_file.open(archive.entries["selected"])

        budget = ExpansionBudget(4)
        budget.consume(4, "selected")
        with self.assertRaisesRegex(ValueError, "expanded zip payload is too large"):
            budget.consume(1, "selected")

        info = zipfile.ZipInfo("selected")
        info.file_size = 1

        class _ArchiveWithUnderstatedMember:
            @staticmethod
            def open(_info: zipfile.ZipInfo, _mode: str) -> io.BytesIO:
                return io.BytesIO(b"12345")

        archive = BudgetedZipFile(
            _ArchiveWithUnderstatedMember(),  # type: ignore[arg-type]
            ExpansionBudget(4),
            MetadataBudget(4),
        )
        with archive.open(info) as source:
            with self.assertRaisesRegex(
                ValueError,
                "expanded zip payload is too large",
            ):
                source.read()

    def test_native_import_never_consumes_materialized_test_data(self) -> None:
        ignored = self._zip(
            [
                ("config/problem.json", b"{}\n"),
                ("test_data/manifest.json", b"x" * 1024),
                ("test_data/tests/001/input", b"y" * 1024),
            ]
        )
        with tempfile.TemporaryDirectory(prefix="native-ignore-") as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            with archive_view_from_bytes(
                ignored,
                max_expanded_bytes=32,
            ) as archive:
                NativePackageImportService().import_package(
                    workspace,
                    "native.zip",
                    archive,
                    text_limit_bytes=256 * 1024,
                )
            self.assertFalse((workspace / "test_data").exists())

        selected = self._zip(
            [
                ("config/problem.json", b"{}\n"),
                ("solutions/large.cpp", b"x" * 1024),
            ]
        )
        with tempfile.TemporaryDirectory(prefix="native-selected-") as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            with archive_view_from_bytes(
                selected,
                max_expanded_bytes=32,
            ) as archive:
                with self.assertRaisesRegex(
                    ValueError,
                    "expanded zip payload is too large",
                ):
                    NativePackageImportService().import_package(
                        workspace,
                        "native.zip",
                        archive,
                        text_limit_bytes=256 * 1024,
                    )
            self.assertEqual(
                list(workspace.parent.glob(".native-import-*")),
                [],
            )
