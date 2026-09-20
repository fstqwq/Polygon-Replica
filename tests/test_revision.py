import unittest

from app.service.repository.revision import parse_verification_source, workspace_verification_source


class TestVerificationSource(unittest.TestCase):
    def test_workspace_source_marker_round_trip(self) -> None:
        base = "a" * 40
        self.assertEqual(workspace_verification_source(base), f"workspace:{base}")
        self.assertEqual(workspace_verification_source(""), "workspace")
        self.assertEqual(parse_verification_source(f"workspace:{base}").kind, "workspace")
        self.assertEqual(parse_verification_source(f"workspace:{base}").base_commit, base)
        self.assertEqual(parse_verification_source(base).kind, "commit")
