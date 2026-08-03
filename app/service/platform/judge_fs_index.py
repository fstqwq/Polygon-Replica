from __future__ import annotations

import contextlib
import os
import re
import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from app.db import now_iso
from app.service.platform.hashing import canonical_json, sha256_hex_bytes, sha256_hex_of_hashes, sha256_hex_text
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _JudgeFsIndexFileMeta(TypedDict):
    size: int
    sha256: str


class _JudgeFsIndexEntry(TypedDict):
    schema: str
    kind: str
    key_hash: str
    signature: str
    value: dict[str, object]
    tags: dict[str, object]
    files: dict[str, _JudgeFsIndexFileMeta]
    integrity_hash: str
    created_at: str
    updated_at: str


class JudgeFsIndexService:
    """Persistent filesystem index for judgehost testcase and verification artifacts.

    Layout:
    - <cache_root>/judge-fs-index/<kind>/<hh>/<key_hash>/<signature>/
    """

    KIND_CASE = "case"
    KIND_VERIFICATION = "verification"

    def __init__(self, cache_root: Path) -> None:
        self._root = (Path(cache_root).resolve() / "judge-fs-index").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._entries_guard = threading.Lock()
        self._key_locks_guard = threading.Lock()
        self._key_locks: dict[tuple[str, str, str], tuple[threading.Lock, int]] = {}
        self._entries: dict[tuple[str, str, str], _JudgeFsIndexEntry] = {}

    @contextlib.contextmanager
    def _key_lock(self, key: tuple[str, str, str]) -> Iterator[None]:
        # File I/O is serialized only for the immutable entry being accessed.
        # Unrelated testcase and result-cache keys must never block each other.
        with self._key_locks_guard:
            lock, users = self._key_locks.get(key, (threading.Lock(), 0))
            self._key_locks[key] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._key_locks_guard:
                current_lock, current_users = self._key_locks[key]
                if current_users == 1:
                    self._key_locks.pop(key)
                else:
                    self._key_locks[key] = (current_lock, current_users - 1)

    @staticmethod
    def signature(payload: object) -> str:
        return sha256_hex_text(canonical_json(payload, ensure_ascii=False))

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        token = kind.strip().lower()
        if token not in {JudgeFsIndexService.KIND_CASE, JudgeFsIndexService.KIND_VERIFICATION}:
            raise RuntimeError("invalid judge fs index kind")
        return token

    @staticmethod
    def _normalize_key_hash(value: str, *, label: str) -> str:
        token = value.strip().lower()
        if not _HEX_64_RE.fullmatch(token):
            raise RuntimeError(f"invalid {label}")
        return token

    @staticmethod
    def _normalize_signature(value: str) -> str:
        token = value.strip().lower()
        if not _HEX_64_RE.fullmatch(token):
            raise RuntimeError("invalid judge fs index signature")
        return token

    @staticmethod
    def _normalize_name(name: str) -> str:
        token = Path(name.strip()).name
        if not _FILE_NAME_RE.fullmatch(token):
            raise RuntimeError("invalid judge fs index file name")
        return token

    def _entry_key(self, *, kind: str, key_hash: str, signature: str) -> tuple[str, str, str]:
        safe_kind = self._normalize_kind(kind)
        safe_key = self._normalize_key_hash(key_hash, label="key hash")
        safe_sig = self._normalize_signature(signature)
        return (safe_kind, safe_key, safe_sig)

    def _entry_dir(self, *, kind: str, key_hash: str, signature: str) -> Path:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        return (self._root / safe_kind / safe_key[:2] / safe_key / safe_sig).resolve()

    def _files_dir(self, *, kind: str, key_hash: str, signature: str) -> Path:
        return (self._entry_dir(kind=kind, key_hash=key_hash, signature=signature) / "files").resolve()

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = (path.parent / f".{path.name}.{os.getpid()}.tmp").resolve()
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _compute_payload_file_index(self, files: dict[str, bytes]) -> tuple[dict[str, _JudgeFsIndexFileMeta], str]:
        file_index: dict[str, _JudgeFsIndexFileMeta] = {}
        file_hashes: list[str] = []
        for name in sorted(files):
            payload = files[name]
            sha = sha256_hex_bytes(payload)
            file_hashes.append(sha)
            file_index[name] = {"size": len(payload), "sha256": sha}
        return (file_index, sha256_hex_of_hashes(file_hashes))

    def _disk_files_match(
        self,
        files_dir: Path,
        expected_files: dict[str, _JudgeFsIndexFileMeta],
    ) -> bool:
        if (not files_dir.exists()) or (not files_dir.is_dir()) or files_dir.is_symlink():
            return False
        disk_sizes: dict[str, int] = {}
        for child in sorted(files_dir.iterdir(), key=lambda item: item.name):
            if (not child.is_file()) or child.is_symlink():
                return False
            name = self._normalize_name(child.name)
            try:
                disk_sizes[name] = child.stat().st_size
            except OSError:
                return False
        return disk_sizes == {
            name: int(meta["size"])
            for name, meta in expected_files.items()
        }

    @staticmethod
    def _clear_integrity_marker_files(entry_dir: Path) -> None:
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return
        for child in list(entry_dir.iterdir()):
            if (not child.is_file()) or child.is_symlink():
                continue
            token = child.name.lower()
            if _HEX_64_RE.fullmatch(token) is None:
                continue
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_integrity_marker(self, entry_dir: Path, integrity_hash: str) -> None:
        if _HEX_64_RE.fullmatch(integrity_hash) is None:
            raise RuntimeError("invalid integrity hash")
        entry_dir.mkdir(parents=True, exist_ok=True)
        self._clear_integrity_marker_files(entry_dir)
        marker = (entry_dir / integrity_hash).resolve()
        if marker.parent != entry_dir:
            raise RuntimeError("invalid integrity marker path")
        self._atomic_write_bytes(marker, b"")

    def _read_integrity_marker(self, entry_dir: Path) -> str:
        if (not entry_dir.exists()) or (not entry_dir.is_dir()) or entry_dir.is_symlink():
            return ""
        tokens: list[str] = []
        for child in sorted(entry_dir.iterdir(), key=lambda item: item.name):
            if (not child.is_file()) or child.is_symlink():
                continue
            token = child.name.lower()
            if _HEX_64_RE.fullmatch(token) is None:
                continue
            tokens.append(token)
        if len(tokens) != 1:
            return ""
        return tokens[0]

    def put(
        self,
        *,
        kind: str,
        key_hash: str,
        signature: str,
        value: dict[str, object] | None,
        files: dict[str, bytes] | None = None,
        tags: dict[str, object] | None = None,
    ) -> None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        value_obj = {} if value is None else dict(value)
        tags_obj = {} if tags is None else dict(tags)
        files_obj = {} if files is None else files
        normalized_files: dict[str, bytes] = {}
        for raw_name, raw_payload in files_obj.items():
            normalized_files[self._normalize_name(raw_name)] = bytes(raw_payload)

        now_text = now_iso()
        key = (safe_kind, safe_key, safe_sig)
        file_index, integrity_hash = self._compute_payload_file_index(normalized_files)
        with self._key_lock(key):
            entry_dir = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            files_dir = (entry_dir / "files").resolve()
            with self._entries_guard:
                old = self._entries.get(key)
            if (
                old is not None
                and old["value"] == value_obj
                and old["tags"] == tags_obj
                and old["files"] == file_index
                and old["integrity_hash"] == integrity_hash
                and self._read_integrity_marker(entry_dir) == integrity_hash
                and self._disk_files_match(files_dir, file_index)
            ):
                return

            created_at = now_text if old is None else old["created_at"]
            replacement = (entry_dir.parent / f".{safe_sig}.{uuid.uuid4().hex}.tmp").resolve()
            replacement_files = (replacement / "files").resolve()
            replacement_files.mkdir(parents=True, exist_ok=False)
            try:
                for name, payload in sorted(normalized_files.items(), key=lambda item: item[0]):
                    target = (replacement_files / name).resolve()
                    if target.parent != replacement_files:
                        raise RuntimeError("invalid judge fs index file path")
                    self._atomic_write_bytes(target, payload)
                self._write_integrity_marker(replacement, integrity_hash)
                if entry_dir.exists() and entry_dir.is_dir() and not entry_dir.is_symlink():
                    shutil.rmtree(entry_dir)
                os.replace(replacement, entry_dir)
            finally:
                if replacement.exists() and replacement.is_dir() and not replacement.is_symlink():
                    shutil.rmtree(replacement, ignore_errors=True)

            row: _JudgeFsIndexEntry = {
                "schema": "judge-fs-index-entry",
                "kind": safe_kind,
                "key_hash": safe_key,
                "signature": safe_sig,
                "value": value_obj,
                "tags": tags_obj,
                "files": file_index,
                "integrity_hash": integrity_hash,
                "created_at": created_at,
                "updated_at": now_text,
            }
            with self._entries_guard:
                self._entries[key] = row

    def get(self, *, kind: str, key_hash: str, signature: str) -> dict[str, object] | None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        key = (safe_kind, safe_key, safe_sig)
        with self._key_lock(key):
            with self._entries_guard:
                row = self._entries.get(key)
            if row is None:
                return None
            entry_dir = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            files_dir = self._files_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            marker_hash = self._read_integrity_marker(entry_dir)
            expected_files = row["files"]
            # Cache hits must not re-hash user-sized blobs. The immutable in-memory
            # index was content-hashed at put time; consumers detect unreadable blobs.
            invalid = marker_hash != row["integrity_hash"] or not self._disk_files_match(
                files_dir,
                expected_files,
            )
            if not invalid:
                return {
                    "schema": row["schema"],
                    "kind": safe_kind,
                    "key_hash": safe_key,
                    "signature": safe_sig,
                    "value": dict(row["value"]),
                    "tags": dict(row["tags"]),
                    "files": {
                        name: dict(meta)
                        for name, meta in expected_files.items()
                    },
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            self._delete_locked(key, entry_dir)
        return None

    def get_with_blobs(
        self,
        *,
        kind: str,
        key_hash: str,
        signature: str,
        names: list[str],
    ) -> tuple[dict[str, object], dict[str, bytes]] | None:
        safe_kind, safe_key, safe_sig = self._entry_key(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
        )
        requested_names = {self._normalize_name(name) for name in names}
        key = (safe_kind, safe_key, safe_sig)
        result: tuple[dict[str, object], dict[str, bytes]] | None = None
        with self._key_lock(key):
            with self._entries_guard:
                row = self._entries.get(key)
            if row is None:
                return None
            entry_dir = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            files_dir = self._files_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            marker_hash = self._read_integrity_marker(entry_dir)
            expected_files = row["files"]
            invalid = marker_hash != row["integrity_hash"] or not self._disk_files_match(
                files_dir,
                expected_files,
            )
            blobs: dict[str, bytes] = {}
            if not invalid:
                # Result-cache consumers need several small artifacts together;
                # keep validation and reads under one stable index snapshot.
                for name in sorted(requested_names.intersection(expected_files)):
                    target = (files_dir / name).resolve()
                    if target.parent != files_dir:
                        invalid = True
                        break
                    try:
                        payload = target.read_bytes()
                    except OSError:
                        invalid = True
                        break
                    if len(payload) != int(expected_files[name]["size"]):
                        invalid = True
                        break
                    blobs[name] = payload
            if not invalid:
                result = (
                    {
                        "schema": row["schema"],
                        "kind": safe_kind,
                        "key_hash": safe_key,
                        "signature": safe_sig,
                        "value": dict(row["value"]),
                        "tags": dict(row["tags"]),
                        "files": {
                            name: dict(meta)
                            for name, meta in expected_files.items()
                        },
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    },
                    blobs,
                )
            if invalid:
                self._delete_locked(key, entry_dir)
        return result

    def read_blob(self, *, kind: str, key_hash: str, signature: str, name: str) -> bytes | None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        safe_name = self._normalize_name(name)
        files_dir = self._files_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
        target = (files_dir / safe_name).resolve()
        if target.parent != files_dir:
            return None
        key = (safe_kind, safe_key, safe_sig)
        with self._key_lock(key):
            if (not target.exists()) or (not target.is_file()) or target.is_symlink():
                return None
            try:
                return target.read_bytes()
            except OSError:
                return None

    def delete(self, *, kind: str, key_hash: str, signature: str) -> None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        key = (safe_kind, safe_key, safe_sig)
        with self._key_lock(key):
            target = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            self._delete_locked(key, target)

    def _delete_locked(self, key: tuple[str, str, str], target: Path) -> None:
        with self._entries_guard:
            self._entries.pop(key, None)
        try:
            if target.exists() and target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass

    def count_entries(self, *, kind: str) -> int:
        safe_kind = self._normalize_kind(kind)
        with self._entries_guard:
            return sum(
                1
                for kind_token, _key, _signature in self._entries
                if kind_token == safe_kind
            )

    def clear_all(self) -> None:
        # Startup/reset owns this operation; runtime maintenance must delete by key.
        with self._key_locks_guard:
            if self._key_locks:
                raise RuntimeError("cannot clear judge fs index while entries are active")
        with self._entries_guard:
            self._entries.clear()
        try:
            if self._root.exists() and self._root.is_dir() and not self._root.is_symlink():
                shutil.rmtree(self._root, ignore_errors=True)
        except OSError:
            pass
        self._root.mkdir(parents=True, exist_ok=True)
