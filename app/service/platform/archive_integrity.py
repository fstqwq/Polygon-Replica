"""Reusable whole-archive integrity checks for derived package files."""

import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from app.service.platform.hashing import sha256_file

_ARCHIVE_INTEGRITY_CACHE_SIZE = 1024


class ArchiveIntegrityError(ValueError):
    """An archive cannot be consumed with its recorded integrity evidence."""


@dataclass(frozen=True)
class ArchiveDescriptor:
    path: Path
    expected_sha256: str
    expected_size_bytes: int


@dataclass(frozen=True)
class _FileStamp:
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _CacheKey:
    expected_sha256: str
    stamp: _FileStamp


@dataclass
class _Flight:
    event: threading.Event
    error: str = ""


class ArchiveIntegrityVerifier:
    """Verify an unchanged archive once per application process."""

    def __init__(
        self,
        artifacts_root: Path,
    ) -> None:
        self._artifacts_root = artifacts_root.resolve()
        self._lock = threading.Lock()
        self._verified: OrderedDict[_CacheKey, None] = OrderedDict()
        self._flights: dict[_CacheKey, _Flight] = {}

    def verify(self, descriptor: ArchiveDescriptor) -> Path:
        resolved, stamp = self._validated_stamp(descriptor)
        key = _CacheKey(
            expected_sha256=descriptor.expected_sha256,
            stamp=stamp,
        )

        while True:
            with self._lock:
                if key in self._verified:
                    self._verified.move_to_end(key)
                    return resolved
                flight = self._flights.get(key)
                if flight is None:
                    flight = _Flight(event=threading.Event())
                    self._flights[key] = flight
                    owner = True
                else:
                    owner = False
            if owner:
                break
            flight.event.wait()
            if flight.error:
                raise ArchiveIntegrityError(flight.error)

        error = ""
        try:
            actual_sha256 = sha256_file(resolved)
            _, final_stamp = self._validated_stamp(descriptor)
            if final_stamp != stamp:
                raise ArchiveIntegrityError(
                    "archive changed while its checksum was being verified"
                )
            if actual_sha256 != descriptor.expected_sha256:
                raise ArchiveIntegrityError("archive checksum changed")
            with self._lock:
                self._remember_key(key)
            return resolved
        except ArchiveIntegrityError as exc:
            error = str(exc) or type(exc).__name__
            raise
        except OSError as exc:
            error = str(exc) or type(exc).__name__
            raise ArchiveIntegrityError(error) from exc
        finally:
            with self._lock:
                completed = self._flights.pop(key)
                completed.error = error
                completed.event.set()

    def remember_published(self, descriptor: ArchiveDescriptor) -> None:
        """Remember writer-produced evidence without reading the archive again."""

        _, stamp = self._validated_stamp(descriptor)
        key = _CacheKey(
            expected_sha256=descriptor.expected_sha256,
            stamp=stamp,
        )
        with self._lock:
            self._remember_key(key)

    def _validated_stamp(
        self,
        descriptor: ArchiveDescriptor,
    ) -> tuple[Path, _FileStamp]:
        expected_sha256 = descriptor.expected_sha256
        if len(expected_sha256) != 64:
            raise ArchiveIntegrityError("archive checksum record is invalid")
        try:
            bytes.fromhex(expected_sha256)
        except ValueError as exc:
            raise ArchiveIntegrityError("archive checksum record is invalid") from exc

        candidate = descriptor.path
        if candidate.is_symlink():
            raise ArchiveIntegrityError("archive must not be a symbolic link")
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ArchiveIntegrityError("archive is missing") from exc
        if self._artifacts_root not in resolved.parents:
            raise ArchiveIntegrityError("archive path escapes artifacts root")
        try:
            file_stat = os.stat(resolved, follow_symlinks=False)
        except OSError as exc:
            raise ArchiveIntegrityError("archive is missing") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise ArchiveIntegrityError("archive is not a regular file")
        if file_stat.st_size != descriptor.expected_size_bytes:
            raise ArchiveIntegrityError("archive size changed")
        return resolved, _FileStamp(
            path=str(resolved),
            device=int(file_stat.st_dev),
            inode=int(file_stat.st_ino),
            size=int(file_stat.st_size),
            modified_ns=int(file_stat.st_mtime_ns),
            changed_ns=int(file_stat.st_ctime_ns),
        )

    def _remember_key(self, key: _CacheKey) -> None:
        self._verified[key] = None
        self._verified.move_to_end(key)
        while len(self._verified) > _ARCHIVE_INTEGRITY_CACHE_SIZE:
            self._verified.popitem(last=False)
