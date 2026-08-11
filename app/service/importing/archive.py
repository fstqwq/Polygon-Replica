"""Bounded ZIP structure validation and consumed-byte accounting."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat
import struct
from types import TracebackType
from typing import BinaryIO
import zipfile


PROBLEM_ZIP_MAX_ENTRIES = 4096
PACKAGE_METADATA_MAX_BYTES = 4 * 1024 * 1024

_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")
_CENTRAL_STRUCT = struct.Struct("<4s6H3L5H2L")


@dataclass(frozen=True)
class ArchivePolicy:
    """Limits for one logical archive import."""

    max_entries: int
    max_expanded_bytes: int
    max_metadata_bytes: int = PACKAGE_METADATA_MAX_BYTES


@dataclass(frozen=True)
class ProblemImportPolicy:
    """Archive and source-text limits captured for one problem import."""

    archive: ArchivePolicy
    text_limit_bytes: int


def problem_import_policy(
    max_expanded_bytes: int,
    text_limit_bytes: int,
) -> ProblemImportPolicy:
    return ProblemImportPolicy(
        archive=problem_archive_policy(max_expanded_bytes),
        text_limit_bytes=max(1, int(text_limit_bytes)),
    )


def problem_archive_policy(max_expanded_bytes: int) -> ArchivePolicy:
    return ArchivePolicy(
        max_entries=PROBLEM_ZIP_MAX_ENTRIES,
        max_expanded_bytes=int(max_expanded_bytes),
        max_metadata_bytes=PACKAGE_METADATA_MAX_BYTES,
    )


def contest_archive_policy(
    max_problems: int,
    problem_max_expanded_bytes: int,
) -> ArchivePolicy:
    return ArchivePolicy(
        max_entries=int(max_problems) * PROBLEM_ZIP_MAX_ENTRIES,
        max_expanded_bytes=int(max_problems) * int(problem_max_expanded_bytes),
        max_metadata_bytes=PACKAGE_METADATA_MAX_BYTES,
    )


@dataclass(frozen=True)
class ArchiveStructure:
    """Validated central-directory location and member count."""

    entry_count: int
    central_offset: int
    central_size: int


def normalize_archive_path(raw: str) -> str:
    """Return one canonical relative member path."""

    value = str(raw).replace("\\", "/")
    pure = PurePosixPath(value)
    if not value or "\x00" in value or pure.is_absolute():
        raise ValueError(f"invalid zip path: {raw}")
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if not parts or ".." in parts or ":" in parts[0]:
        raise ValueError(f"invalid zip path: {raw}")
    return PurePosixPath(*parts).as_posix()


def _find_eocd(handle: BinaryIO, size: int) -> tuple[int, tuple[object, ...]]:
    tail_size = min(size, 22 + 65535)
    handle.seek(size - tail_size)
    tail = handle.read(tail_size)
    search_end = len(tail)
    while True:
        offset = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if offset < 0:
            raise ValueError("archive end record not found")
        if offset + _EOCD_STRUCT.size <= len(tail):
            record = _EOCD_STRUCT.unpack_from(tail, offset)
            comment_length = int(record[-1])
            if offset + _EOCD_STRUCT.size + comment_length == len(tail):
                return (size - tail_size + offset, record)
        search_end = offset


def _zip64_structure(
    handle: BinaryIO,
    *,
    eocd_offset: int,
) -> ArchiveStructure:
    locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    if locator_offset < 0:
        raise ValueError("ZIP64 locator is missing")
    handle.seek(locator_offset)
    locator_raw = handle.read(_ZIP64_LOCATOR_STRUCT.size)
    if len(locator_raw) != _ZIP64_LOCATOR_STRUCT.size:
        raise ValueError("ZIP64 locator is truncated")
    signature, disk_number, record_offset, total_disks = _ZIP64_LOCATOR_STRUCT.unpack(
        locator_raw
    )
    if signature != _ZIP64_LOCATOR_SIGNATURE:
        raise ValueError("ZIP64 locator is missing")
    if disk_number != 0 or total_disks != 1:
        raise ValueError("multi-disk zip archives are not supported")
    handle.seek(record_offset)
    record_raw = handle.read(_ZIP64_EOCD_STRUCT.size)
    if len(record_raw) != _ZIP64_EOCD_STRUCT.size:
        raise ValueError("ZIP64 end record is truncated")
    record = _ZIP64_EOCD_STRUCT.unpack(record_raw)
    if record[0] != _ZIP64_EOCD_SIGNATURE or int(record[1]) < 44:
        raise ValueError("ZIP64 end record is malformed")
    record_end = int(record_offset) + 12 + int(record[1])
    if int(record_offset) < 0 or record_end > locator_offset:
        raise ValueError("ZIP64 end record is out of bounds")
    disk, central_disk = int(record[4]), int(record[5])
    entries_disk, entries_total = int(record[6]), int(record[7])
    if disk != 0 or central_disk != 0 or entries_disk != entries_total:
        raise ValueError("multi-disk zip archives are not supported")
    structure = ArchiveStructure(
        entry_count=entries_total,
        central_size=int(record[8]),
        central_offset=int(record[9]),
    )
    if structure.central_offset + structure.central_size > int(record_offset):
        raise ValueError("zip central directory overlaps ZIP64 end records")
    return structure


def _classic_structure(record: tuple[object, ...]) -> ArchiveStructure:
    disk, central_disk = int(record[1]), int(record[2])
    entries_disk, entries_total = int(record[3]), int(record[4])
    if disk != 0 or central_disk != 0 or entries_disk != entries_total:
        raise ValueError("multi-disk zip archives are not supported")
    return ArchiveStructure(
        entry_count=entries_total,
        central_size=int(record[5]),
        central_offset=int(record[6]),
    )


def _decode_member_name(raw: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & 0x800 else "cp437"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError("zip entry name is not decodable") from exc


def _extra_fields(raw: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    offset = 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            raise ValueError("zip central-directory extra field is truncated")
        field_id, field_size = struct.unpack_from("<HH", raw, offset)
        offset += 4
        end = offset + int(field_size)
        if end > len(raw):
            raise ValueError("zip central-directory extra field is truncated")
        if field_id == 0x0001 and field_id in fields:
            raise ValueError("duplicate ZIP64 central-directory extra field")
        fields.setdefault(int(field_id), raw[offset:end])
        offset = end
    return fields


def _resolved_central_location(
    fields: tuple[object, ...],
    extra_raw: bytes,
) -> tuple[int, int]:
    """Resolve ZIP64 placeholders needed for disk and local-header checks."""

    uncompressed_size = int(fields[9])
    compressed_size = int(fields[8])
    local_offset = int(fields[16])
    disk_start = int(fields[13])
    if not any(
        (
            uncompressed_size == 0xFFFFFFFF,
            compressed_size == 0xFFFFFFFF,
            local_offset == 0xFFFFFFFF,
            disk_start == 0xFFFF,
        )
    ):
        return local_offset, disk_start
    payload = _extra_fields(extra_raw).get(0x0001)
    if payload is None:
        raise ValueError("ZIP64 central-directory entry is missing its extra field")
    cursor = 0

    def consume(width: int) -> int:
        nonlocal cursor
        end = cursor + width
        if end > len(payload):
            raise ValueError("ZIP64 central-directory extra field is truncated")
        value = int.from_bytes(payload[cursor:end], "little")
        cursor = end
        return value

    if uncompressed_size == 0xFFFFFFFF:
        consume(8)
    if compressed_size == 0xFFFFFFFF:
        consume(8)
    if local_offset == 0xFFFFFFFF:
        local_offset = consume(8)
    if disk_start == 0xFFFF:
        disk_start = consume(4)
    return local_offset, disk_start


def preflight_archive(path: Path, *, max_entries: int) -> ArchiveStructure:
    """Validate and count the central directory before ``ZipFile`` allocation."""

    size = path.stat().st_size
    if size < _EOCD_STRUCT.size:
        raise ValueError("archive is not a valid zip")
    with path.open("rb") as handle:
        eocd_offset, record = _find_eocd(handle, size)
        zip64 = any(
            value == sentinel
            for value, sentinel in (
                (int(record[3]), 0xFFFF),
                (int(record[4]), 0xFFFF),
                (int(record[5]), 0xFFFFFFFF),
                (int(record[6]), 0xFFFFFFFF),
            )
        )
        structure = (
            _zip64_structure(handle, eocd_offset=eocd_offset)
            if zip64
            else _classic_structure(record)
        )
        entry_limit = max(1, int(max_entries))
        if structure.entry_count > entry_limit:
            raise ValueError(f"zip contains more than {entry_limit} entries")
        central_end = structure.central_offset + structure.central_size
        if (
            structure.central_offset < 0
            or structure.central_size < 0
            or central_end > eocd_offset
        ):
            raise ValueError("zip central directory is out of bounds")
        handle.seek(structure.central_offset)
        consumed = 0
        count = 0
        seen: dict[str, bool] = {}
        while consumed < structure.central_size:
            fixed = handle.read(_CENTRAL_STRUCT.size)
            if len(fixed) != _CENTRAL_STRUCT.size:
                raise ValueError("zip central directory is truncated")
            fields = _CENTRAL_STRUCT.unpack(fixed)
            if fields[0] != _CENTRAL_SIGNATURE:
                raise ValueError("zip central directory contains an invalid record")
            flags = int(fields[3])
            name_length = int(fields[10])
            extra_length = int(fields[11])
            comment_length = int(fields[12])
            variable_length = name_length + extra_length + comment_length
            variable = handle.read(variable_length)
            if len(variable) != variable_length:
                raise ValueError("zip central directory is truncated")
            extra_start = name_length
            extra_end = extra_start + extra_length
            local_offset, disk_start = _resolved_central_location(
                fields,
                variable[extra_start:extra_end],
            )
            if disk_start != 0:
                raise ValueError("multi-disk zip archives are not supported")
            if local_offset < 0 or local_offset + 30 > structure.central_offset:
                raise ValueError("zip local-header offset is out of bounds")
            central_position = handle.tell()
            handle.seek(local_offset)
            if handle.read(4) != b"PK\x03\x04":
                raise ValueError("zip local-header signature is invalid")
            handle.seek(central_position)
            raw_name = _decode_member_name(variable[:name_length], flags)
            name = normalize_archive_path(raw_name)
            if name in seen:
                raise ValueError(f"duplicate zip path: {name}")
            is_directory = raw_name.endswith("/")
            seen[name] = is_directory
            count += 1
            if count > entry_limit:
                raise ValueError(f"zip contains more than {entry_limit} entries")
            consumed += _CENTRAL_STRUCT.size + variable_length
        if consumed != structure.central_size or count != structure.entry_count:
            raise ValueError("zip central-directory entry count is inconsistent")
        for name in seen:
            parts = PurePosixPath(name).parts
            for index in range(1, len(parts)):
                ancestor = PurePosixPath(*parts[:index]).as_posix()
                if ancestor in seen and not seen[ancestor]:
                    raise ValueError(
                        f"conflicting zip paths: {ancestor} and {name}"
                    )
        return structure


class ExpansionBudget:
    """Track declared selected bytes and actual decompressor output."""

    def __init__(self, maximum: int, *, parent: ExpansionBudget | None = None) -> None:
        self.maximum = max(1, int(maximum))
        self._parent = parent
        self._declared = 0
        self._actual = 0
        self._selected: set[int] = set()

    def select(self, info: zipfile.ZipInfo) -> None:
        identity = id(info)
        if identity in self._selected:
            return
        self._check_select(info)
        if self._parent is not None:
            self._parent.select(info)
        self._selected.add(identity)
        self._declared += max(0, int(info.file_size))

    def _check_select(self, info: zipfile.ZipInfo) -> None:
        identity = id(info)
        if identity in self._selected:
            return
        declared = max(0, int(info.file_size))
        filename = info.filename
        if self._declared + declared > self.maximum:
            raise ValueError(f"expanded zip payload is too large at {filename}")
        if self._parent is not None:
            self._parent._check_select(info)  # pylint: disable=protected-access

    def consume(self, amount: int, filename: str) -> None:
        increment = max(0, int(amount))
        if self._actual + increment > self.maximum:
            raise ValueError(f"expanded zip payload is too large at {filename}")
        if self._parent is not None and self._parent._actual + increment > self._parent.maximum:  # pylint: disable=protected-access
            raise ValueError(f"expanded zip payload is too large at {filename}")
        if self._parent is not None:
            self._parent.consume(increment, filename)
        self._actual += increment


class MetadataBudget:
    """Bound concurrently retained in-memory metadata for one import."""

    def __init__(self, maximum: int) -> None:
        self.maximum = max(1, int(maximum))
        self._used = 0

    def consume(self, amount: int, label: str) -> None:
        increment = max(0, int(amount))
        if self._used + increment > self.maximum:
            raise ValueError(f"package metadata is too large at {label}")
        self._used += increment


class _BudgetedReader:
    def __init__(self, raw: zipfile.ZipExtFile, budget: ExpansionBudget, filename: str):
        self._raw = raw
        self._budget = budget
        self._filename = filename

    def __enter__(self) -> _BudgetedReader:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        payload = self._raw.read(size)
        self._budget.consume(len(payload), self._filename)
        return payload

    def close(self) -> None:
        self._raw.close()


class BudgetedZipFile:
    """Narrow ``ZipFile`` facade whose reads consume an expansion budget."""

    def __init__(
        self,
        archive: zipfile.ZipFile,
        budget: ExpansionBudget,
        metadata_budget: MetadataBudget,
    ):
        self._archive = archive
        self._budget = budget
        self._metadata_budget = metadata_budget

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._archive.infolist()

    def open(self, member: str | zipfile.ZipInfo, mode: str = "r") -> _BudgetedReader:
        if mode != "r":
            raise ValueError("archive import is read-only")
        info = self._archive.getinfo(member) if isinstance(member, str) else member
        self._validate_selected_entry(info)
        self._budget.select(info)
        return _BudgetedReader(self._archive.open(info, "r"), self._budget, info.filename)

    def read_metadata(
        self,
        info: zipfile.ZipInfo,
        *,
        limit: int,
        label: str | None = None,
    ) -> bytes:
        cap = max(1, int(limit))
        display = label or info.filename
        if int(info.file_size) > cap:
            raise ValueError(f"metadata is too large: {display}")
        with self.open(info) as source:
            payload = source.read(cap + 1)
        if len(payload) > cap:
            raise ValueError(f"metadata is too large: {display}")
        self._metadata_budget.consume(len(payload), display)
        return payload

    def copy_to(
        self,
        info: zipfile.ZipInfo,
        target: Path,
        *,
        normalize_newlines: bool = False,
    ) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        pending_cr = False
        with self.open(info) as source, target.open("wb") as destination:
            while chunk := source.read(1024 * 1024):
                if normalize_newlines:
                    if pending_cr:
                        chunk = b"\r" + chunk
                        pending_cr = False
                    if chunk.endswith(b"\r"):
                        chunk = chunk[:-1]
                        pending_cr = True
                    chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                destination.write(chunk)
                written += len(chunk)
            if pending_cr:
                destination.write(b"\n")
                written += 1
        return written

    def copy_canonical_text_to(self, info: zipfile.ZipInfo, target: Path) -> int:
        """Stream UTF-8 text with canonical manual-input whitespace rules."""

        target.parent.mkdir(parents=True, exist_ok=True)
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending_cr = False
        document_content_end = 0
        line_content_end = 0
        with self.open(info) as source, target.open("w+b") as destination:
            while chunk := source.read(1024 * 1024):
                decoder.decode(chunk, final=False)
                if pending_cr:
                    chunk = b"\r" + chunk
                    pending_cr = False
                if chunk.endswith(b"\r"):
                    chunk = chunk[:-1]
                    pending_cr = True
                chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                segments = chunk.split(b"\n")
                for segment in segments[:-1]:
                    start = destination.tell()
                    destination.write(segment)
                    stripped = segment.rstrip(b" \t")
                    if stripped:
                        line_content_end = start + len(stripped)
                        document_content_end = line_content_end
                    destination.seek(line_content_end)
                    destination.truncate()
                    destination.write(b"\n")
                    line_content_end = destination.tell()
                tail = segments[-1]
                start = destination.tell()
                destination.write(tail)
                stripped_tail = tail.rstrip(b" \t")
                if stripped_tail:
                    line_content_end = start + len(stripped_tail)
                    document_content_end = line_content_end
            if pending_cr:
                destination.seek(line_content_end)
                destination.truncate()
                destination.write(b"\n")
            decoder.decode(b"", final=True)
            destination.seek(document_content_end)
            destination.truncate()
            destination.write(b"\n")
            return destination.tell()

    @staticmethod
    def _validate_selected_entry(info: zipfile.ZipInfo) -> None:
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted zip entry is not supported: {info.filename}")
        mode = (int(info.external_attr) >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError(f"zip symlink is not allowed: {info.filename}")
        if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(f"zip special file is not allowed: {info.filename}")


class ArchiveView:
    """A validated archive with a canonical logical root."""

    def __init__(
        self,
        path: Path,
        policy: ArchivePolicy,
        *,
        _archive: zipfile.ZipFile | None = None,
        _budget: ExpansionBudget | None = None,
        _metadata_budget: MetadataBudget | None = None,
        _entries: dict[str, zipfile.ZipInfo] | None = None,
        _owner: bool = True,
    ) -> None:
        self.path = path
        self.policy = policy
        self._owner = _owner
        if _archive is None:
            structure = preflight_archive(path, max_entries=policy.max_entries)
            archive = zipfile.ZipFile(path, "r")
            if len(archive.infolist()) != structure.entry_count:
                archive.close()
                raise ValueError("zip entry count changed after preflight")
            entries: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                normalized = normalize_archive_path(info.filename)
                if normalized in entries:
                    archive.close()
                    raise ValueError(f"duplicate zip path: {normalized}")
                entries[normalized] = info
            self._archive = archive
            self._budget = ExpansionBudget(policy.max_expanded_bytes)
            self._metadata_budget = MetadataBudget(policy.max_metadata_bytes)
            self._entries = entries
        else:
            self._archive = _archive
            self._budget = _budget or ExpansionBudget(policy.max_expanded_bytes)
            self._metadata_budget = _metadata_budget or MetadataBudget(
                policy.max_metadata_bytes
            )
            self._entries = dict(_entries or {})
        self.zip_file = BudgetedZipFile(
            self._archive, self._budget, self._metadata_budget
        )

    def __enter__(self) -> ArchiveView:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def entries(self) -> dict[str, zipfile.ZipInfo]:
        return dict(self._entries)

    def rooted_at(self, anchor: str) -> ArchiveView:
        safe_anchor = normalize_archive_path(anchor)
        if safe_anchor in self._entries:
            prefix = ""
        else:
            suffix = "/" + safe_anchor
            roots = {
                name[: -len(safe_anchor)]
                for name in self._entries
                if name.endswith(suffix)
            }
            if len(roots) != 1:
                raise ValueError(f"{safe_anchor} not found at package root")
            prefix = next(iter(roots))
            root_directory = prefix.rstrip("/")
            if any(
                not (
                    name.startswith(prefix)
                    or (
                        name == root_directory
                        and self._entries[name].is_dir()
                    )
                )
                for name in self._entries
            ):
                raise ValueError("archive contains files outside its package root")
        entries = {
            name.removeprefix(prefix): info
            for name, info in self._entries.items()
            if name.startswith(prefix) and name.removeprefix(prefix)
        }
        if safe_anchor not in entries:
            raise ValueError(f"{safe_anchor} not found at package root")
        return ArchiveView(
            self.path,
            self.policy,
            _archive=self._archive,
            _budget=self._budget,
            _metadata_budget=self._metadata_budget,
            _entries=entries,
            _owner=False,
        )

    def subview(self, prefix: str, policy: ArchivePolicy) -> ArchiveView:
        """Create a child archive view with local and parent expansion budgets."""

        safe_prefix = normalize_archive_path(prefix).rstrip("/") + "/"
        entries = {
            name.removeprefix(safe_prefix): info
            for name, info in self._entries.items()
            if name.startswith(safe_prefix) and name != safe_prefix.rstrip("/")
        }
        if not entries:
            raise ValueError(f"archive folder is empty or missing: {prefix}")
        if len(entries) > int(policy.max_entries):
            raise ValueError(
                f"problem package contains more than {int(policy.max_entries)} entries"
            )
        return ArchiveView(
            self.path,
            policy,
            _archive=self._archive,
            _budget=ExpansionBudget(policy.max_expanded_bytes, parent=self._budget),
            _metadata_budget=self._metadata_budget,
            _entries=entries,
            _owner=False,
        )

    def read_metadata(self, info: zipfile.ZipInfo, *, label: str | None = None) -> bytes:
        return self.zip_file.read_metadata(
            info,
            limit=self.policy.max_metadata_bytes,
            label=label,
        )

    def close(self) -> None:
        if self._owner:
            self._archive.close()
