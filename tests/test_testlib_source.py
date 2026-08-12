from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.impl.problem.compile_check import _testlib_extra_sources
from app.service.platform.testlib_source import maintained_testlib_header, workspace_testlib_header
from app.service.platform.runtime_blob_store import PayloadFile
from tests.common import WorkspaceTestBase, runtime


class TestWorkspaceTestlibSource(WorkspaceTestBase):
    def test_workspace_seed_copies_maintained_testlib(self) -> None:
        workspace = self._workspace_path()
        workspace_header = workspace / "third_party" / "testlib" / "testlib.h"
        upstream_header = maintained_testlib_header(repo_root=Path(__file__).resolve().parents[1])
        self.assertEqual(workspace_header.read_bytes(), upstream_header.read_bytes())


class TestTestlibSourceHelpers(unittest.TestCase):
    def test_workspace_testlib_header_returns_workspace_file_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            header = workspace / "third_party" / "testlib" / "testlib.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text("// workspace testlib\n", encoding="utf-8")
            self.assertEqual(workspace_testlib_header(workspace), header.resolve())

    def test_compile_check_extra_sources_use_workspace_header_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            header = workspace / "third_party" / "testlib" / "testlib.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text("// workspace testlib\n", encoding="utf-8")

            payload = _testlib_extra_sources(
                runtime,
                workspace,
                "validators/validator.cpp",
            )
            self.assertIsInstance(payload, dict)
            extra = payload.get("extra_source_files") if isinstance(payload, dict) else None
            self.assertIsInstance(extra, dict)
            self.assertEqual(
                runtime.runtime_blob_store.read(PayloadFile.from_payload(extra["testlib.h"])),
                b"// workspace testlib\n",
            )

    def test_compile_check_extra_sources_do_not_replace_missing_workspace_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.assertIsNone(
                _testlib_extra_sources(
                    runtime,
                    workspace,
                    "validators/validator.cpp",
                )
            )


if __name__ == "__main__":
    unittest.main()
