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
        self.service._write_cache_integrity_marker(p)
        os.utime(p, (mtime, mtime))
        return p

    def test_cleanup_enforces_entry_limit_low_heat_first(self) -> None:
        now = time.time()
        cold = self._write_cache_bin(f"digest1/{'a' * 64}/binary.bin", size=8, mtime=now - 600)
        warm = self._write_cache_bin(f"digest1/{'b' * 64}/binary.bin", size=8, mtime=now - 300)
        hot = self._write_cache_bin(f"digest1/{'c' * 64}/binary.bin", size=8, mtime=now - 5)

        self.service.cache_max_entries = 2
        self.service.cache_max_bytes = 0

        cleaned = self.service.cleanup_cache(force=True)
        self.assertTrue(cleaned)
        self.assertFalse(cold.exists())
        self.assertTrue(warm.exists())
        self.assertTrue(hot.exists())

    def test_cleanup_enforces_size_limit_low_heat_first(self) -> None:
        now = time.time()
        cold = self._write_cache_bin(f"digest2/{'d' * 64}/binary.bin", size=10, mtime=now - 120)
        hot = self._write_cache_bin(f"digest2/{'e' * 64}/binary.bin", size=10, mtime=now - 10)

        self.service.cache_max_entries = 0
        self.service.cache_max_bytes = 10

        cleaned = self.service.cleanup_cache(force=True)
        self.assertTrue(cleaned)
        self.assertFalse(cold.exists())
        self.assertTrue(hot.exists())

    def test_cleanup_prunes_orphan_sidecars(self) -> None:
        entry_root = self.service.cache_root / "digest3" / ("f" * 64)
        entry_root.mkdir(parents=True, exist_ok=True)
        orphan_marker = entry_root / ("0" * 64)
        orphan_lock = entry_root / "binary.lock"
        orphan_marker.write_bytes(b"")
        orphan_lock.write_text("", encoding="utf-8")

        self.service.cache_max_entries = 0
        self.service.cache_max_bytes = 0
        cleaned = self.service.cleanup_cache(force=True)
        self.assertTrue(cleaned)
        self.assertFalse(orphan_marker.exists())
        self.assertFalse(orphan_lock.exists())
        self.assertFalse(entry_root.exists())

    def test_cleanup_does_not_remove_active_lock_without_binary(self) -> None:
        entry_root = self.service.cache_root / "digest4" / ("1" * 64)
        entry_root.mkdir(parents=True, exist_ok=True)
        active_lock = entry_root / "binary.lock"
        active_lock.write_text("", encoding="utf-8")
        lock_handle = self.service._acquire_file_lock(active_lock, nonblocking=False)
        try:
            self.service.cache_max_entries = 0
            self.service.cache_max_bytes = 0
            cleaned = self.service.cleanup_cache(force=True)
            self.assertTrue(cleaned)
            self.assertTrue(active_lock.exists())
            self.assertTrue(entry_root.exists())
        finally:
            try:
                lock_handle.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
