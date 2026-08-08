from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.service.repository.revision import (
    parse_verification_source,
    verification_source_display,
    workspace_upstream_revision_display,
    workspace_verification_source,
)


class TestVerificationSource(unittest.TestCase):
    def test_workspace_upstream_revision_display(self) -> None:
        self.assertEqual(
            workspace_upstream_revision_display(2, 3),
            "Workspace on v2 / Upstream v3",
        )
        self.assertEqual(
            workspace_upstream_revision_display(None, None),
            "none / Upstream missing",
        )
        self.assertEqual(
            workspace_upstream_revision_display(1, None),
            "Workspace on v1 / Upstream missing",
        )

    def test_workspace_source_marker_round_trip(self) -> None:
        base = "a" * 40
        self.assertEqual(workspace_verification_source(base), f"workspace:{base}")
        self.assertEqual(workspace_verification_source(""), "workspace")
        self.assertEqual(parse_verification_source(f"workspace:{base}").kind, "workspace")
        self.assertEqual(parse_verification_source(f"workspace:{base}").base_commit, base)
        self.assertEqual(parse_verification_source(base).kind, "commit")

    def test_workspace_source_display_keeps_snapshot_semantics(self) -> None:
        workspace = Path("/tmp/polygon-replica-test-workspace")
        cache: dict[str, int | None] = {}
        base = "b" * 40
        with patch("app.service.repository.revision.git_commit_count", return_value=7):
            self.assertEqual(
                verification_source_display(workspace, f"workspace:{base}", cache),
                "Workspace on v7",
            )
            self.assertEqual(
                verification_source_display(workspace, base, cache),
                "Published v7",
            )
        self.assertEqual(verification_source_display(workspace, "workspace", cache), "Workspace")
