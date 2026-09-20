import hashlib
from pathlib import Path
from urllib.parse import quote


class StaticAssetManifest:
    """Immutable startup manifest for cache-safe static asset URLs."""

    def __init__(self, static_root: Path, *, digest_length: int = 12) -> None:
        if not 8 <= digest_length <= 64:
            raise ValueError("static asset digest length must be between 8 and 64")
        self._root = static_root.resolve(strict=True)
        self._digest_length = digest_length
        self._urls = self._build_urls()

    def _build_urls(self) -> dict[str, str]:
        urls: dict[str, str] = {}
        for file_path in sorted(self._root.rglob("*")):
            if not file_path.is_file() or not file_path.resolve().is_relative_to(self._root):
                continue
            relative_path = file_path.relative_to(self._root).as_posix()
            if "\\" in relative_path:
                continue
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            urls[relative_path] = f"/static/{quote(relative_path, safe='/')}?v={digest[:self._digest_length]}"
        return urls

    def url(self, asset_path: str) -> str:
        if not isinstance(asset_path, str):
            raise TypeError("static asset path must be a string")
        url = self._urls.get(asset_path)
        if url is None:
            raise ValueError(f"unknown static asset: {asset_path}")
        return url
