from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.service.judgehost.file_stream import DomjudgeDownloadFile, stream_domjudge_file_array
from app.service.platform.runtime_blob_store import RuntimeBlobStore
from app.service.platform.runtime_cache_index import RuntimeCacheIndex
from app.service.verification.signature import verification_manifest


class TestRuntimeBlobStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="runtime-blobs-")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.blobs = RuntimeBlobStore(self.root / "runtime")

    def test_bytes_and_file_with_same_content_share_one_blob(self) -> None:
        payload = b"shared testcase payload\n"
        source = self.root / "001.in"
        source.write_bytes(payload)
        from_bytes = self.blobs.put_bytes(payload)
        from_file = self.blobs.put_file(source)
        self.assertEqual(from_file.blob_ref, from_bytes.blob_ref)
        self.assertEqual(from_file.path, from_bytes.path)

    def test_domjudge_json_stream_preserves_base64_chunk_boundaries(self) -> None:
        payloads = [b"", b"a", b"ab", b"abc", bytes(range(251)) * 66842]
        files = []
        for index, payload in enumerate(payloads):
            files.append(
                DomjudgeDownloadFile(
                    filename=f"file-{index}",
                    payload=self.blobs.put_bytes(payload),
                    is_executable=index % 2 == 0,
                )
            )
        encoded = b"".join(stream_domjudge_file_array(files))
        rows = json.loads(encoded)
        self.assertEqual(
            [base64.b64decode(row["content"]) for row in rows],
            payloads,
        )

    def test_manifest_with_git_identities_does_not_hash_file_content(self) -> None:
        config_dir = self.root / "config"
        config_dir.mkdir()
        source = config_dir / "problem.json"
        source.write_bytes(b'{"time_limit_ms":2000}\n')
        identity = "a" * 64
        with patch(
            "app.service.verification.signature.sha256_file",
            side_effect=AssertionError("clean manifest read file content"),
        ):
            manifest = verification_manifest(
                self.root,
                git_identities={"config/problem.json": identity},
            )
        self.assertEqual(manifest.require("config/problem.json").identity, identity)

    def test_dirty_manifest_detects_same_size_change(self) -> None:
        manual_dir = self.root / "tests" / "manual"
        manual_dir.mkdir(parents=True)
        source = manual_dir / "001.in"
        source.write_bytes(b"AAAA")
        first = verification_manifest(self.root)
        source.write_bytes(b"BBBB")
        second = verification_manifest(self.root)
        self.assertNotEqual(first.signature, second.signature)
        self.assertNotEqual(
            first.require("tests/manual/001.in").identity,
            second.require("tests/manual/001.in").identity,
        )

    def test_cache_index_discards_entry_when_blob_size_changes(self) -> None:
        index = RuntimeCacheIndex(self.blobs)
        entry = index.put(
            namespace=RuntimeCacheIndex.RESULT,
            key_hash="1" * 64,
            signature="2" * 64,
            value={"status": "ok"},
            files={"program.out": b"answer\n"},
        )
        entry.files["program.out"].path.write_bytes(b"broken")

        self.assertIsNone(
            index.get(
                namespace=RuntimeCacheIndex.RESULT,
                key_hash="1" * 64,
                signature="2" * 64,
            )
        )
        self.assertEqual(index.count_entries(namespace=RuntimeCacheIndex.RESULT), 0)


if __name__ == "__main__":
    unittest.main()
