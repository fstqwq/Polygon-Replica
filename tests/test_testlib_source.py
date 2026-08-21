import tempfile
import unittest
from pathlib import Path

from app.service.platform.testlib_source import maintained_testlib_header, workspace_testlib_header
from tests.common import WorkspaceTestBase


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

if __name__ == "__main__":
    unittest.main()
