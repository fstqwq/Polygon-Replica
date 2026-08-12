from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_REF_RE = re.compile(r"^blob://sha256/(?P<identity>[0-9a-f]{64})$")
_COPY_CHUNK_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PayloadFile:
    path: Path
    size: int
    identity: str
    blob_ref: str | None = None

    def __post_init__(self) -> None:
        identity = self.identity.lower()
        if _SHA256_RE.fullmatch(identity) is None:
            raise ValueError("payload identity must be a SHA-256 digest")
        if self.size < 0:
            raise ValueError("payload size cannot be negative")
        object.__setattr__(self, "path", self.path.resolve())
        object.__setattr__(self, "identity", identity)

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "size": self.size,
            "identity": self.identity,
            "blob_ref": self.blob_ref,
        }

    @classmethod
    def from_payload(cls, raw: object) -> PayloadFile:
        payload = cast(dict[str, object], raw)
        return cls(
            path=Path(str(payload["path"])),
            size=int(payload["size"]),
            identity=str(payload["identity"]),
            blob_ref=None if payload.get("blob_ref") is None else str(payload["blob_ref"]),
        )


class RuntimeBlobStore:
    """Process-lifetime immutable content store.

    Runtime cache indexes may forget entries independently. Blob references remain
    valid until the startup-owned runtime root is cleared.
    """

    def __init__(self, blob_root: Path) -> None:
        self._root = Path(blob_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    @staticmethod
    def ref(identity: str) -> str:
        safe_identity = RuntimeBlobStore.normalize_identity(identity)
        return f"blob://sha256/{safe_identity}"

    @staticmethod
    def parse_ref(blob_ref: str) -> str | None:
        match = _BLOB_REF_RE.fullmatch(blob_ref)
        return None if match is None else match.group("identity")

    @staticmethod
    def normalize_identity(identity: str) -> str:
        token = identity.lower()
        if _SHA256_RE.fullmatch(token) is None:
            raise ValueError("invalid runtime blob identity")
        return token

    def _path(self, identity: str) -> Path:
        safe_identity = self.normalize_identity(identity)
        return (self._root / safe_identity[:2] / safe_identity).resolve()

    @contextlib.contextmanager
    def _key_lock(self, identity: str) -> Iterator[None]:
        safe_identity = self.normalize_identity(identity)
        with self._locks_guard:
            lock, users = self._locks.get(safe_identity, (threading.Lock(), 0))
            self._locks[safe_identity] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._locks_guard:
                current_lock, current_users = self._locks[safe_identity]
                if current_users == 1:
                    self._locks.pop(safe_identity)
                else:
                    self._locks[safe_identity] = (current_lock, current_users - 1)

    @staticmethod
    def describe_file(path: Path, *, identity: str | None = None) -> PayloadFile:
        source = Path(path).resolve()
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(source)
        stat_obj = source.stat()
        digest = identity
        if digest is None:
            hasher = hashlib.sha256()
            with source.open("rb") as handle:
                while chunk := handle.read(_COPY_CHUNK_SIZE):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        return PayloadFile(path=source, size=int(stat_obj.st_size), identity=digest)

    def put_bytes(self, payload: bytes) -> PayloadFile:
        blob = bytes(payload)
        identity = hashlib.sha256(blob).hexdigest()
        target = self._path(identity)
        with self._key_lock(identity):
            if not self._valid_target(target, len(blob)):
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = (target.parent / f".{identity}.{uuid.uuid4().hex}.tmp").resolve()
                try:
                    temp.write_bytes(blob)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        return PayloadFile(
            path=target,
            size=len(blob),
            identity=identity,
            blob_ref=self.ref(identity),
        )

    def put_file(self, payload: PayloadFile | Path) -> PayloadFile:
        descriptor = payload if isinstance(payload, PayloadFile) else self.describe_file(payload)
        target = self._path(descriptor.identity)
        with self._key_lock(descriptor.identity):
            if not self._valid_target(target, descriptor.size):
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = (target.parent / f".{descriptor.identity}.{uuid.uuid4().hex}.tmp").resolve()
                try:
                    try:
                        os.link(descriptor.path, temp)
                    except OSError:
                        with descriptor.path.open("rb") as source, temp.open("xb") as destination:
                            shutil.copyfileobj(source, destination, length=_COPY_CHUNK_SIZE)
                    if temp.stat().st_size != descriptor.size:
                        raise OSError("runtime blob source changed while copying")
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        return PayloadFile(
            path=target,
            size=descriptor.size,
            identity=descriptor.identity,
            blob_ref=self.ref(descriptor.identity),
        )

    def descriptor(self, blob_ref: str) -> PayloadFile | None:
        identity = self.parse_ref(blob_ref)
        if identity is None:
            return None
        path = self._path(identity)
        try:
            if path.is_symlink() or not path.is_file():
                return None
            size = int(path.stat().st_size)
        except OSError:
            return None
        return PayloadFile(path=path, size=size, identity=identity, blob_ref=blob_ref)

    @contextlib.contextmanager
    def open(self, payload: PayloadFile | str) -> Iterator[BinaryIO]:
        descriptor = self._require_descriptor(payload)
        handle = descriptor.path.open("rb")
        try:
            yield handle
        finally:
            handle.close()

    def read(self, payload: PayloadFile | str, *, max_bytes: int | None = None) -> bytes:
        descriptor = self._require_descriptor(payload)
        if max_bytes is not None and descriptor.size > max_bytes:
            raise ValueError("runtime blob exceeds read limit")
        with descriptor.path.open("rb") as handle:
            return handle.read()

    def read_tail(self, payload: PayloadFile | str, *, max_bytes: int) -> bytes:
        descriptor = self._require_descriptor(payload)
        length = min(descriptor.size, max(0, int(max_bytes)))
        with descriptor.path.open("rb") as handle:
            handle.seek(descriptor.size - length)
            return handle.read(length)

    def copy_to(self, payload: PayloadFile | str, destination: Path) -> None:
        descriptor = self._require_descriptor(payload)
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with descriptor.path.open("rb") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=_COPY_CHUNK_SIZE)

    def clear_all(self) -> None:
        with self._locks_guard:
            if self._locks:
                raise RuntimeError("cannot clear runtime blobs while entries are active")
        if self._root.exists() and self._root.is_dir() and not self._root.is_symlink():
            shutil.rmtree(self._root)
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _valid_target(path: Path, expected_size: int) -> bool:
        try:
            return path.is_file() and not path.is_symlink() and path.stat().st_size == expected_size
        except OSError:
            return False

    def _require_descriptor(self, payload: PayloadFile | str) -> PayloadFile:
        descriptor = payload if isinstance(payload, PayloadFile) else self.descriptor(payload)
        if descriptor is None:
            raise FileNotFoundError("runtime blob is unavailable")
        if not self._valid_target(descriptor.path, descriptor.size):
            raise FileNotFoundError(descriptor.path)
        return descriptor
