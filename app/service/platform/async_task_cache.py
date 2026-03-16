from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path
from typing import TypedDict

from app.db import now_iso
from app.service.platform.hashing import canonical_json, sha256_hex_bytes, sha256_hex_of_hashes


_CACHE_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CACHE_KEY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class _AsyncTaskCacheEntry(TypedDict):
    namespace: str
    key_hash: str
    key_json: str
    value: dict[str, object]
    tags: dict[str, object]
    created_at: str
    updated_at: str
    last_hit_at: str
    hit_count: int


class AsyncTaskCacheService:
    def __init__(self, _db: object, cache_root: Path) -> None:
        self._root = (Path(cache_root).resolve() / "async-task-cache").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _AsyncTaskCacheEntry] = {}

    def _normalize_namespace(self, namespace: str) -> str:
        token = namespace.strip().lower()
        if not _CACHE_NAMESPACE_RE.fullmatch(token):
            raise RuntimeError("invalid async cache namespace")
        return token

    def key_hash(self, key_parts: object) -> str:
        return sha256_hex_bytes(canonical_json(key_parts, ensure_ascii=False).encode("utf-8"))

    def _entry_dir(self, namespace: str, key_hash: str) -> Path:
        safe_namespace = self._normalize_namespace(namespace)
        safe_hash = key_hash.strip().lower()
        if not _CACHE_KEY_HASH_RE.fullmatch(safe_hash):
            raise RuntimeError("invalid async cache key hash")
        return (self._root / safe_namespace / safe_hash[:2] / safe_hash).resolve()

    @staticmethod
    def _atomic_touch(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = (path.parent / f".{path.name}.{os.getpid()}.tmp").resolve()
        try:
            tmp.write_bytes(b"")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _clear_integrity_marker_files(entry_dir: Path) -> None:
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return
        for child in list(entry_dir.iterdir()):
            if (not child.is_file()) or child.is_symlink():
                continue
            token = child.name.lower()
            if _CACHE_KEY_HASH_RE.fullmatch(token) is None:
                continue
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_integrity_marker(self, namespace: str, key_hash: str, integrity_hash: str) -> None:
        safe_integrity_hash = integrity_hash.strip().lower()
        if _CACHE_KEY_HASH_RE.fullmatch(safe_integrity_hash) is None:
            raise RuntimeError("invalid async cache integrity hash")
        entry_dir = self._entry_dir(namespace, key_hash)
        entry_dir.mkdir(parents=True, exist_ok=True)
        self._clear_integrity_marker_files(entry_dir)
        marker = (entry_dir / safe_integrity_hash).resolve()
        if marker.parent != entry_dir:
            raise RuntimeError("invalid async cache integrity marker path")
        self._atomic_touch(marker)

    def _read_integrity_marker(self, namespace: str, key_hash: str) -> str:
        entry_dir = self._entry_dir(namespace, key_hash)
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return ""
        found: list[str] = []
        for child in sorted(entry_dir.iterdir(), key=lambda item: item.name):
            if (not child.is_file()) or child.is_symlink():
                continue
            token = child.name.lower()
            if _CACHE_KEY_HASH_RE.fullmatch(token):
                found.append(token)
        if len(found) != 1:
            return ""
        return found[0]

    def _delete_files_for_hash(self, namespace: str, key_hash: str) -> None:
        try:
            directory = self._entry_dir(namespace, key_hash)
        except Exception:
            return
        try:
            if directory.exists() and directory.is_dir() and (not directory.is_symlink()):
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            pass

    def _entry_integrity_hash(self, meta: _AsyncTaskCacheEntry) -> str:
        meta_text = canonical_json(meta, ensure_ascii=False)
        h_meta = sha256_hex_bytes(meta_text.encode("utf-8"))
        return sha256_hex_of_hashes([h_meta])

    def get(self, namespace: str, key_parts: object) -> dict[str, object] | None:
        safe_namespace = self._normalize_namespace(namespace)
        key_json = canonical_json(key_parts, ensure_ascii=False)
        key_hash = sha256_hex_bytes(key_json.encode("utf-8"))
        entry_key = (safe_namespace, key_hash)
        with self._lock:
            meta = self._entries.get(entry_key)
            if meta is None:
                return None
            expected_integrity = self._entry_integrity_hash(meta)
            marker = self._read_integrity_marker(safe_namespace, key_hash)
            if marker != expected_integrity:
                self._entries.pop(entry_key, None)
                self._delete_files_for_hash(safe_namespace, key_hash)
                return None
            now_text = now_iso()
            refreshed_meta: _AsyncTaskCacheEntry = {
                **meta,
                "last_hit_at": now_text,
                "updated_at": now_text,
                "hit_count": meta["hit_count"] + 1,
            }
            refreshed_integrity = self._entry_integrity_hash(refreshed_meta)
            self._entries[entry_key] = refreshed_meta
            self._write_integrity_marker(safe_namespace, key_hash, refreshed_integrity)
            return {
                "namespace": safe_namespace,
                "key_hash": key_hash,
                "key_json": key_json,
                "value": dict(refreshed_meta["value"]),
                "tags": dict(refreshed_meta["tags"]),
                "dir": self._entry_dir(safe_namespace, key_hash),
            }

    def put(
        self,
        namespace: str,
        key_parts: object,
        value: dict[str, object] | None,
        *,
        tags: dict[str, object] | None = None,
    ) -> str:
        safe_namespace = self._normalize_namespace(namespace)
        key_json = canonical_json(key_parts, ensure_ascii=False)
        key_hash = sha256_hex_bytes(key_json.encode("utf-8"))
        now_text = now_iso()
        entry_key = (safe_namespace, key_hash)
        safe_value = {} if value is None else dict(value)
        safe_tags = {} if tags is None else dict(tags)
        with self._lock:
            prev = self._entries.get(entry_key)
            created_at = now_text if prev is None else prev["created_at"]
            last_hit_at = "" if prev is None else prev["last_hit_at"]
            hit_count = 0 if prev is None else prev["hit_count"]
            meta: _AsyncTaskCacheEntry = {
                "namespace": safe_namespace,
                "key_hash": key_hash,
                "key_json": key_json,
                "value": safe_value,
                "tags": safe_tags,
                "created_at": created_at,
                "updated_at": now_text,
                "last_hit_at": last_hit_at,
                "hit_count": hit_count,
            }
            self._entries[entry_key] = meta
            self._write_integrity_marker(safe_namespace, key_hash, self._entry_integrity_hash(meta))
        return key_hash

    def delete(self, namespace: str, key_parts: object) -> None:
        safe_namespace = self._normalize_namespace(namespace)
        key_hash = sha256_hex_bytes(canonical_json(key_parts, ensure_ascii=False).encode("utf-8"))
        self.delete_by_hash(safe_namespace, key_hash)

    def delete_by_hash(self, namespace: str, key_hash: str) -> None:
        safe_namespace = self._normalize_namespace(namespace)
        safe_hash = key_hash.strip().lower()
        if not _CACHE_KEY_HASH_RE.fullmatch(safe_hash):
            return
        with self._lock:
            self._entries.pop((safe_namespace, safe_hash), None)
            self._delete_files_for_hash(safe_namespace, safe_hash)

    def entry_dir(self, namespace: str, key_parts: object) -> Path:
        safe_namespace = self._normalize_namespace(namespace)
        key_hash = sha256_hex_bytes(canonical_json(key_parts, ensure_ascii=False).encode("utf-8"))
        return self._entry_dir(safe_namespace, key_hash)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            try:
                if self._root.exists() and self._root.is_dir() and (not self._root.is_symlink()):
                    shutil.rmtree(self._root, ignore_errors=True)
            except OSError:
                pass
            self._root.mkdir(parents=True, exist_ok=True)
