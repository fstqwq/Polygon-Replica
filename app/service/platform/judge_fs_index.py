from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import TypedDict

from app.db import now_iso
from app.service.platform.hashing import canonical_json, sha256_hex_bytes, sha256_hex_of_hashes, sha256_hex_text
from app.service.platform.rwlock import WriterPriorityRWLock


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
        self._lock = WriterPriorityRWLock()
        self._entries: dict[tuple[str, str, str], _JudgeFsIndexEntry] = {}

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

    @staticmethod
    def _empty_dir_files(path: Path) -> None:
        if (not path.exists()) or (not path.is_dir()) or path.is_symlink():
            return
        for child in list(path.iterdir()):
            try:
                if child.is_file() and (not child.is_symlink()):
                    child.unlink(missing_ok=True)
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
        with self._lock.write_lock():
            entry_dir = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            files_dir = self._files_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            entry_dir.mkdir(parents=True, exist_ok=True)
            files_dir.mkdir(parents=True, exist_ok=True)
            self._empty_dir_files(files_dir)
            for name, payload in sorted(normalized_files.items(), key=lambda item: item[0]):
                target = (files_dir / name).resolve()
                if target.parent != files_dir:
                    raise RuntimeError("invalid judge fs index file path")
                self._atomic_write_bytes(target, payload)

            file_index, integrity_hash = self._compute_payload_file_index(normalized_files)
            self._write_integrity_marker(entry_dir, integrity_hash)

            key = (safe_kind, safe_key, safe_sig)
            old = self._entries.get(key)
            created_at = now_text if old is None else old["created_at"]
            self._entries[key] = {
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

    def get(self, *, kind: str, key_hash: str, signature: str) -> dict[str, object] | None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        key = (safe_kind, safe_key, safe_sig)
        invalid = False
        with self._lock.read_lock():
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
        if invalid:
            self.delete(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
        return None

    def read_blob(self, *, kind: str, key_hash: str, signature: str, name: str) -> bytes | None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        safe_name = self._normalize_name(name)
        files_dir = self._files_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
        target = (files_dir / safe_name).resolve()
        if target.parent != files_dir:
            return None
        with self._lock.read_lock():
            if (not target.exists()) or (not target.is_file()) or target.is_symlink():
                return None
            try:
                return target.read_bytes()
            except OSError:
                return None

    def delete(self, *, kind: str, key_hash: str, signature: str) -> None:
        safe_kind, safe_key, safe_sig = self._entry_key(kind=kind, key_hash=key_hash, signature=signature)
        with self._lock.write_lock():
            self._entries.pop((safe_kind, safe_key, safe_sig), None)
            target = self._entry_dir(kind=safe_kind, key_hash=safe_key, signature=safe_sig)
            try:
                if target.exists() and target.is_dir() and (not target.is_symlink()):
                    shutil.rmtree(target, ignore_errors=True)
            except OSError:
                pass

    def count_entries(self, *, kind: str) -> int:
        safe_kind = self._normalize_kind(kind)
        root = (self._root / safe_kind).resolve()
        if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            return 0
        with self._lock.read_lock():
            total = 0
            for files_dir in root.rglob("files"):
                if (not files_dir.exists()) or (not files_dir.is_dir()) or files_dir.is_symlink():
                    continue
                entry_dir = files_dir.parent
                marker = self._read_integrity_marker(entry_dir)
                if marker:
                    total += 1
            return total

    def clear_all(self) -> None:
        with self._lock.write_lock():
            self._entries.clear()
            try:
                if self._root.exists() and self._root.is_dir() and (not self._root.is_symlink()):
                    shutil.rmtree(self._root, ignore_errors=True)
            except OSError:
                pass
            self._root.mkdir(parents=True, exist_ok=True)
