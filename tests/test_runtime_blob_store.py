from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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

    def test_manifest_identity_is_always_the_file_content_sha256(self) -> None:
        config_dir = self.root / "config"
        config_dir.mkdir()
        source = config_dir / "problem.json"
        content = b'{"time_limit_ms":2000}\n'
        source.write_bytes(content)

        manifest = verification_manifest(self.root)
        descriptor = manifest.require("config/problem.json")

        self.assertEqual(descriptor.identity, hashlib.sha256(content).hexdigest())
        self.assertEqual(
            self.blobs.put_file(descriptor).blob_ref,
            self.blobs.put_bytes(content).blob_ref,
        )

    def test_unrelated_file_does_not_change_manifest_file_identity(self) -> None:
        solutions_dir = self.root / "solutions"
        solutions_dir.mkdir()
        source = solutions_dir / "std.cpp"
        content = b"int main() { return 0; }\n"
        source.write_bytes(content)

        first = verification_manifest(self.root)
        (solutions_dir / "extra.py").write_text("print('extra')\n", encoding="utf-8")
        second = verification_manifest(self.root)

        expected = hashlib.sha256(content).hexdigest()
        self.assertEqual(first.require("solutions/std.cpp").identity, expected)
        self.assertEqual(second.require("solutions/std.cpp").identity, expected)

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
