from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from app.service.platform.hashing import sha256_hex_bytes


_EXECUTABLE_KIND_RE = re.compile(r"^(compile|run|compare)$")
_EXECUTABLE_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_EXECUTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DomjudgeExecutableCache:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        token = str(kind or "").strip().lower()
        if _EXECUTABLE_KIND_RE.fullmatch(token) is None:
            raise RuntimeError("invalid executable kind")
        return token

    @staticmethod
    def _normalize_hash(executable_hash: str) -> str:
        token = str(executable_hash or "").strip().lower()
        if _EXECUTABLE_HASH_RE.fullmatch(token) is None:
            raise RuntimeError("invalid executable hash")
        return token

    @staticmethod
    def _normalize_name(name: str) -> str:
        token = Path(str(name or "").strip()).name
        if _EXECUTABLE_NAME_RE.fullmatch(token) is None:
            raise RuntimeError("invalid executable file name")
        return token

    def _entry_dir(self, *, kind: str, executable_hash: str) -> Path:
        safe_kind = self._normalize_kind(kind)
        safe_hash = self._normalize_hash(executable_hash)
        return (self._root / safe_kind / safe_hash).resolve()

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
    def _manifest_rows(files: list[tuple[str, bytes, bool]]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for raw_name, raw_content, raw_is_exec in sorted(files, key=lambda item: str(item[0])):
            name = DomjudgeExecutableCache._normalize_name(raw_name)
            content = bytes(raw_content or b"")
            rows.append(
                {
                    "filename": name,
                    "is_executable": bool(raw_is_exec),
                    "sha256": sha256_hex_bytes(content),
                    "size": len(content),
                }
            )
        return rows

    def put(self, *, kind: str, executable_hash: str, files: list[tuple[str, bytes, bool]]) -> None:
        safe_kind = self._normalize_kind(kind)
        safe_hash = self._normalize_hash(executable_hash)
        entry_dir = self._entry_dir(kind=safe_kind, executable_hash=safe_hash)
        files_dir = (entry_dir / "files").resolve()
        manifest_rows = self._manifest_rows(files)
        content_by_name: dict[str, tuple[bytes, bool]] = {}
        for raw_name, raw_content, raw_is_exec in files:
            name = self._normalize_name(raw_name)
            content_by_name[name] = (bytes(raw_content or b""), bool(raw_is_exec))
        manifest_path = (entry_dir / "manifest.json").resolve()
        with self._lock:
            files_dir.mkdir(parents=True, exist_ok=True)
            for child in list(files_dir.iterdir()):
                if child.is_file() and (not child.is_symlink()):
                    child.unlink(missing_ok=True)
            for row in manifest_rows:
                name = str(row["filename"])
                content, is_exec = content_by_name[name]
                target = (files_dir / name).resolve()
                if target.parent != files_dir:
                    raise RuntimeError("invalid executable cache file path")
                self._atomic_write_bytes(target, content)
                if is_exec:
                    target.chmod(int(target.stat().st_mode) | 0o755)
            manifest_payload = json.dumps(
                {"schema": "domjudge-executable-cache-v1", "files": manifest_rows},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._atomic_write_bytes(manifest_path, manifest_payload)

    def read(self, *, kind: str, executable_hash: str) -> list[dict[str, object]] | None:
        safe_kind = self._normalize_kind(kind)
        safe_hash = self._normalize_hash(executable_hash)
        entry_dir = self._entry_dir(kind=safe_kind, executable_hash=safe_hash)
        files_dir = (entry_dir / "files").resolve()
        manifest_path = (entry_dir / "manifest.json").resolve()
        with self._lock:
            if (not manifest_path.exists()) or (not files_dir.exists()):
                return None
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            rows_raw = manifest.get("files")
            if not isinstance(rows_raw, list):
                return None
            out: list[dict[str, object]] = []
            for raw_row in rows_raw:
                if not isinstance(raw_row, dict):
                    return None
                filename = self._normalize_name(str(raw_row.get("filename") or ""))
                target = (files_dir / filename).resolve()
                if target.parent != files_dir or (not target.exists()) or (not target.is_file()):
                    return None
                blob = target.read_bytes()
                if sha256_hex_bytes(blob) != str(raw_row.get("sha256") or ""):
                    return None
                if len(blob) != int(raw_row.get("size") or 0):
                    return None
                out.append(
                    {
                        "filename": filename,
                        "content": blob,
                        "is_executable": bool(raw_row.get("is_executable")),
                    }
                )
            return out

    def delete(self, *, kind: str, executable_hash: str) -> bool:
        safe_kind = self._normalize_kind(kind)
        safe_hash = self._normalize_hash(executable_hash)
        entry_dir = self._entry_dir(kind=safe_kind, executable_hash=safe_hash)
        kind_root = (self._root / safe_kind).resolve()
        if entry_dir.parent != kind_root:
            raise RuntimeError("invalid executable cache entry path")
        with self._lock:
            if not entry_dir.exists():
                return False
            if entry_dir.is_symlink() or not entry_dir.is_dir():
                return False
            shutil.rmtree(entry_dir, ignore_errors=False)
            return True
