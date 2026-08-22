from pathlib import Path
from unittest.mock import patch

from app.impl.contest.statement_review import (
    contest_statement_pdf_page,
    contest_statement_review_build,
    contest_statement_review_page,
)
from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_one
from tests.ui_support import (
    runtime,
    contest_access_revoke,
    _request,
    workspace_service,
)


class TestContestAccessActions(ContestActionBase):
    def test_contest_reader_can_render_statement_review_and_pdf(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "reader-preview"
        )
        _row_id, _problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "reader-preview-problem",
        )
        reader = "carol"
        workspace_service.ensure_user(reader)
        runtime.contest_service.grant_member_role(contest_id, reader, "read")
        workspace_service.ensure_workspace(problem_slug, reader, refresh_status=False)
        reader_row = workspace_service.user_row(reader)
        access = runtime.access_query.contest_context(contest_id, int(reader_row["id"]))
        self.assertTrue(access["can_read"])
        self.assertFalse(access["can_build"])

        preview = {
            "id": "sp-contest-reader",
            "status": "ok",
            "summary": {"items": []},
            "language": "english",
        }
        pdf_path = Path(runtime.settings.cache_root) / "contest-reader.pdf"
        pdf_path.write_bytes(b"%PDF-reader")
        self.addCleanup(pdf_path.unlink, missing_ok=True)
        request = _request(f"/contests/{contest_slug}/statements/review")

        with (
            patch.object(
                runtime.contest_statement_service,
                "resolve_language",
                return_value="english",
            ),
            patch.object(
                runtime.contest_statement_preview_service,
                "build_html",
                return_value=preview,
            ) as build_html,
            patch.object(
                runtime.contest_statement_preview_service,
                "build_pdf",
                return_value=preview,
            ) as build_pdf,
            patch.object(
                runtime.statement_preview_service,
                "pdf",
                return_value=pdf_path,
            ),
        ):
            review_page = contest_statement_review_page(
                request,
                contest_slug,
                reader,
                source="workspace",
                language="english",
            )
            review_build = contest_statement_review_build(
                request,
                contest_slug,
                reader,
                source="workspace",
                language="english",
            )
            pdf_page = contest_statement_pdf_page(
                request,
                contest_slug,
                reader,
                source="workspace",
                language="english",
            )
        self.assertEqual(review_page.status_code, 200)
        self.assertEqual(review_build.status_code, 303)
        self.assertEqual(pdf_page.status_code, 200)
        self.assertEqual(build_html.call_count, 2)
        self.assertEqual(build_pdf.call_count, 1)

    def test_membership_only_revoke_preserves_problem_acl(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("revoke-membership")
        _row_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "membership-problem",
        )
        workspace_service.ensure_user("carol")
        runtime.contest_service.grant_member_role(contest_id, "carol", "read")
        workspace_service.grant_repo_access(problem_slug, "carol", "read")

        response = contest_access_revoke(contest=contest_slug, user="alice", target_user="carol")

        self.assertEqual(response.status_code, 303)
        acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=(SELECT id FROM users WHERE username='carol')",
            [problem_id],
        )
        self.assertIsNotNone(acl)
        self.assertEqual(str(acl["role"]), "read")

    def test_membership_grants_and_revokes_problem_access_without_repo_acl_rows(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("dynamic-access")
        _row_a, problem_id, _problem_slug = self.add_owned_problem(
            contest_id, actor_user_id, "A", "dynamic-problem"
        )
        workspace_service.ensure_user("bob")
        runtime.contest_service.grant_member_role(contest_id, "bob", "write")
        bob = db_fetch_one("SELECT id FROM users WHERE username='bob'")
        self.assertIsNotNone(bob)
        self.assertTrue(
            runtime.access_query.problem_context(problem_id, int(bob["id"]))["can_write"]
        )
        self.assertIn(
            _problem_slug,
            runtime.workspace_service.accessible_problem_slugs(int(bob["id"]), limit=20),
        )
        participating = runtime.workspace_service.participating_problem_rows(
            int(bob["id"]), limit=20
        )
        self.assertIn(_problem_slug, [str(row["slug"]) for row in participating])
        self.assertIsNone(
            db_fetch_one(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [problem_id, int(bob["id"])],
            )
        )

        response = contest_access_revoke(contest=contest_slug, user="alice", target_user="bob")

        self.assertEqual(response.status_code, 303)
        self.assertFalse(
            runtime.access_query.problem_context(problem_id, int(bob["id"]))["can_read"]
        )
        self.assertNotIn(
            _problem_slug,
            runtime.workspace_service.accessible_problem_slugs(int(bob["id"]), limit=20),
        )
