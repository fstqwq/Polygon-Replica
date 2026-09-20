import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.service.platform.workspace_path import is_hidden_workspace_path, safe_workspace_path
from app.service.problem.test_spec import parse_gen_command_tokens


class TestWorkspacePathSecurity(unittest.TestCase):
    def test_hidden_workspace_paths_are_rejected(self) -> None:
        self.assertTrue(is_hidden_workspace_path((".env",)))
        self.assertTrue(is_hidden_workspace_path(("notes", ".cache", "secret.txt")))
        self.assertTrue(is_hidden_workspace_path((".gitignore",)))
        self.assertFalse(is_hidden_workspace_path(("notes", "readme.txt")))

        with tempfile.TemporaryDirectory(prefix="workspace-path-") as temp_dir:
            workspace = Path(temp_dir)
            for rel_path in (".env", "notes/.cache/secret.txt"):
                with self.subTest(rel_path=rel_path), self.assertRaises(HTTPException) as denied:
                    safe_workspace_path(workspace, rel_path)
                self.assertEqual(denied.exception.status_code, 400)
                self.assertEqual(str(denied.exception.detail), "hidden path is not allowed")

    def test_generator_command_metacharacters_remain_plain_tokens(self) -> None:
        self.assertEqual(
            parse_gen_command_tokens('gen.cpp 7 && touch "file with spaces"'),
            ["gen.cpp", "7", "&&", "touch", "file with spaces"],
        )
