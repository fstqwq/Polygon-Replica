import re
from dataclasses import dataclass

from app.service.judgehost.domjudge.codec import decode_basename, decode_text
from app.service.platform.hashing import sha256_hex_text
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.runtime_cache_index import RuntimeCacheIndex


@dataclass(frozen=True, slots=True)
class ExecutableCacheFile:
    filename: str
    payload: PayloadFile
    is_executable: bool


class ExecutableCache:
    """Store DOMjudge executable file sets in the process-local runtime cache."""

    _KINDS = frozenset({"compile", "run", "compare"})
    _HASH_RE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(self, index: RuntimeCacheIndex) -> None:
        self._index = index

    @classmethod
    def _identity(cls, kind: str, executable_hash: str) -> tuple[str, str]:
        safe_kind = decode_text(lower=True, raw=kind)
        safe_hash = decode_text(lower=True, raw=executable_hash)
        if safe_kind not in cls._KINDS or cls._HASH_RE.fullmatch(safe_hash) is None:
            raise RuntimeError("invalid executable cache identity")
        return (safe_kind, safe_hash)

    @staticmethod
    def _key_hash(kind: str, executable_hash: str) -> str:
        return sha256_hex_text(f"{kind}\0{executable_hash}")

    def store(
        self,
        *,
        kind: str,
        executable_hash: str,
        files: tuple[tuple[str, bytes, bool], ...],
    ) -> dict[str, PayloadFile]:
        safe_kind, safe_hash = self._identity(kind, executable_hash)
        file_payloads: dict[str, bytes] = {}
        manifest: list[dict[str, object]] = []
        for name, content, is_executable in sorted(files, key=lambda item: item[0]):
            safe_name = decode_basename(raw=name)
            if not safe_name or safe_name in file_payloads:
                raise RuntimeError("invalid executable cache file set")
            file_payloads[safe_name] = bytes(content)
            manifest.append({"filename": safe_name, "is_executable": bool(is_executable)})
        entry = self._index.put(
            namespace=RuntimeCacheIndex.EXECUTABLE,
            key_hash=self._key_hash(safe_kind, safe_hash),
            signature=safe_hash,
            value={
                "kind": safe_kind,
                "executable_hash": safe_hash,
                "files": manifest,
            },
            files=file_payloads,
            tags={"artifact_kind": "domjudge-executable", "executable_kind": safe_kind},
        )
        return dict(entry.files)

    def read(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> tuple[ExecutableCacheFile, ...] | None:
        safe_kind, safe_hash = self._identity(kind, executable_hash)
        entry = self._index.get(
            namespace=RuntimeCacheIndex.EXECUTABLE,
            key_hash=self._key_hash(safe_kind, safe_hash),
            signature=safe_hash,
        )
        if entry is None:
            return None
        value = dict(entry.value)
        if (
            value.get("kind") != safe_kind
            or value.get("executable_hash") != safe_hash
        ):
            return None
        manifest = value.get("files")
        if not isinstance(manifest, list):
            return None
        rows: list[ExecutableCacheFile] = []
        for raw_row in manifest:
            if not isinstance(raw_row, dict):
                return None
            filename = decode_basename(raw=raw_row.get("filename"))
            if not filename or filename not in entry.files:
                return None
            rows.append(
                ExecutableCacheFile(
                    filename=filename,
                    payload=entry.files[filename],
                    is_executable=bool(raw_row.get("is_executable")),
                )
            )
        if len(rows) != len(entry.files):
            return None
        return tuple(rows)
