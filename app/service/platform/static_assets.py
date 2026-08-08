from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, quote


class StaticAssetManifest:
    """Immutable startup manifest for cache-safe static asset URLs."""

    def __init__(self, static_root: Path, *, digest_length: int = 12) -> None:
        if not 8 <= digest_length <= 64:
            raise ValueError("static asset digest length must be between 8 and 64")
        self._root = static_root.resolve(strict=True)
        self._digest_length = digest_length
        self._digests = self._build_digests()

    def _build_digests(self) -> dict[str, str]:
        digests: dict[str, str] = {}
        for file_path in sorted(self._root.rglob("*")):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(self._root).as_posix()
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            digests[relative_path] = digest[: self._digest_length]
        return digests

    def url(self, asset_path: str) -> str:
        canonical_path = self._canonical_path(asset_path)
        digest = self._digests.get(canonical_path)
        if digest is None:
            raise ValueError(f"unknown static asset: {canonical_path}")
        encoded_path = quote(canonical_path, safe="/")
        return f"/static/{encoded_path}?{urlencode({'v': digest})}"

    def _canonical_path(self, asset_path: str) -> str:
        if not isinstance(asset_path, str):
            raise TypeError("static asset path must be a string")
        if not asset_path or asset_path.startswith("/") or "\\" in asset_path:
            raise ValueError("static asset path must be a relative POSIX path")
        if any(part in {"", ".", ".."} for part in asset_path.split("/")):
            raise ValueError("static asset path contains an invalid segment")
        pure_path = PurePosixPath(asset_path)
        canonical_path = pure_path.as_posix()
        resolved_path = (self._root / canonical_path).resolve(strict=False)
        if not resolved_path.is_relative_to(self._root):
            raise ValueError("static asset path escapes the static root")
        return canonical_path
