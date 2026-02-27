from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from app.services.sandbox.native_backend import NativeSandboxBackend
from app.services.toolchain_service import ToolchainService


class TestToolchainCacheCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = ToolchainService(self.root, sandbox_backend=NativeSandboxBackend())
        self.service.cache_cleanup_interval_sec = 0

    def _write_cache_bin(self, rel: str, size: int, mtime: float) -> Path:
        p = self.service.cache_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_cleanup_removes_expired_entries(self) -> None:
        now = time.time()
        expired = self._write_cache_bin("digest1/expired.bin", size=8, mtime=now - 600)
        recent = self._write_cache_bin("digest1/recent.bin", size=8, mtime=now - 5)

        self.service.cache_ttl_sec = 60
        self.service.cache_max_bytes = 0

        cleaned = self.service.cleanup_cache(force=True)
        self.assertTrue(cleaned)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())

    def test_cleanup_enforces_size_limit_oldest_first(self) -> None:
        now = time.time()
        oldest = self._write_cache_bin("digest2/oldest.bin", size=10, mtime=now - 120)
        newer = self._write_cache_bin("digest2/newer.bin", size=10, mtime=now - 10)

        self.service.cache_ttl_sec = 0
        self.service.cache_max_bytes = 10

        cleaned = self.service.cleanup_cache(force=True)
        self.assertTrue(cleaned)
        self.assertFalse(oldest.exists())
        self.assertTrue(newer.exists())


if __name__ == "__main__":
    unittest.main()
