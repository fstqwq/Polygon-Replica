import contextlib
import hashlib
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from app.db import now_iso
from app.service.platform.hashing import canonical_json, sha256_hex_text
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore


_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RuntimeCacheConflictError(RuntimeError):
    """A cache identity was already published with different content."""


@dataclass(frozen=True, slots=True)
class RuntimeCacheEntry:
    namespace: str
    key_hash: str
    signature: str
    value: dict[str, object]
    tags: dict[str, object]
    files: dict[str, PayloadFile]
    created_at: str
    updated_at: str


class RuntimeCacheIndex:
    RESULT = "result"
    EXECUTABLE = "executable"
    _NAMESPACES = frozenset({RESULT, EXECUTABLE})

    def __init__(self, blob_store: RuntimeBlobStore) -> None:
        self._blob_store = blob_store
        self._entries_guard = threading.Lock()
        self._entries: dict[tuple[str, str, str], RuntimeCacheEntry] = {}
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple[str, str, str], tuple[threading.Lock, int]] = {}

    @staticmethod
    def signature(payload: object) -> str:
        return sha256_hex_text(canonical_json(payload, ensure_ascii=False))

    @contextlib.contextmanager
    def key_lock(self, namespace: str, key_hash: str, signature: str) -> Iterator[None]:
        key = self._entry_key(namespace, key_hash, signature)
        with self._locks_guard:
            lock, users = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._locks_guard:
                current_lock, current_users = self._locks[key]
                if current_users == 1:
                    self._locks.pop(key)
                else:
                    self._locks[key] = (current_lock, current_users - 1)

    def put(
        self,
        *,
        namespace: str,
        key_hash: str,
        signature: str,
        value: dict[str, object],
        files: Mapping[str, bytes | PayloadFile],
        tags: dict[str, object] | None = None,
    ) -> RuntimeCacheEntry:
        key = self._entry_key(namespace, key_hash, signature)
        normalized_files = {
            self._normalize_name(name): payload
            for name, payload in files.items()
        }
        with self.key_lock(*key):
            with self._entries_guard:
                current = self._entries.get(key)
            if current is not None and self._entry_is_valid(current):
                expected_tags = {} if tags is None else dict(tags)
                expected_files = {
                    name: (
                        (payload.identity, payload.size)
                        if isinstance(payload, PayloadFile)
                        else (hashlib.sha256(payload).hexdigest(), len(payload))
                    )
                    for name, payload in normalized_files.items()
                }
                current_files = {
                    name: (payload.identity, payload.size)
                    for name, payload in current.files.items()
                }
                if current.value != value or current.tags != expected_tags or current_files != expected_files:
                    raise RuntimeCacheConflictError(
                        "runtime cache identity maps to different content"
                    )
                return current
            stored_files = {
                name: (
                    self._blob_store.put_file(payload)
                    if isinstance(payload, PayloadFile)
                    else self._blob_store.put_bytes(payload)
                )
                for name, payload in normalized_files.items()
            }
            now_text = now_iso()
            entry = RuntimeCacheEntry(
                namespace=key[0],
                key_hash=key[1],
                signature=key[2],
                value=dict(value),
                tags={} if tags is None else dict(tags),
                files=stored_files,
                created_at=now_text if current is None else current.created_at,
                updated_at=now_text,
            )
            with self._entries_guard:
                self._entries[key] = entry
            return entry

    def get(self, *, namespace: str, key_hash: str, signature: str) -> RuntimeCacheEntry | None:
        key = self._entry_key(namespace, key_hash, signature)
        with self.key_lock(*key):
            with self._entries_guard:
                entry = self._entries.get(key)
            if entry is None:
                return None
            if self._entry_is_valid(entry):
                return entry
            with self._entries_guard:
                self._entries.pop(key, None)
        return None

    def delete(self, *, namespace: str, key_hash: str, signature: str) -> None:
        key = self._entry_key(namespace, key_hash, signature)
        with self.key_lock(*key):
            with self._entries_guard:
                self._entries.pop(key, None)

    def count_entries(self, *, namespace: str) -> int:
        safe_namespace = self._normalize_namespace(namespace)
        with self._entries_guard:
            return sum(1 for key in self._entries if key[0] == safe_namespace)

    def clear_all(self) -> None:
        with self._locks_guard:
            if self._locks:
                raise RuntimeError("cannot clear runtime cache index while entries are active")
        with self._entries_guard:
            self._entries.clear()

    def _entry_is_valid(self, entry: RuntimeCacheEntry) -> bool:
        for payload in entry.files.values():
            descriptor = self._blob_store.descriptor(payload.blob_ref or "")
            if descriptor is None or descriptor.size != payload.size:
                return False
        return True

    @classmethod
    def _entry_key(cls, namespace: str, key_hash: str, signature: str) -> tuple[str, str, str]:
        return (
            cls._normalize_namespace(namespace),
            cls._normalize_hash(key_hash, "cache key"),
            cls._normalize_hash(signature, "cache signature"),
        )

    @classmethod
    def _normalize_namespace(cls, namespace: str) -> str:
        token = namespace.lower()
        if token not in cls._NAMESPACES:
            raise ValueError("invalid runtime cache namespace")
        return token

    @staticmethod
    def _normalize_hash(value: str, label: str) -> str:
        token = value.lower()
        if _HEX_64_RE.fullmatch(token) is None:
            raise ValueError(f"invalid {label}")
        return token

    @staticmethod
    def _normalize_name(name: str) -> str:
        if _FILE_NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid runtime cache file name")
        return name
