import asyncio
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.impl.contest.statement_review import (
    contest_statement_pdf_page,
    contest_statement_review_build,
    contest_statement_review_page,
)
from app.impl.contest.package import contest_packages_download
from tests.contest_support import ContestActionBase
from tests.db_helpers import db_fetch_one
from tests.ui_support import (
    _request,
    _post_form_request,
    contest_access_grant,
    contest_access_revoke,
    contest_problem_access_save,
    runtime,
    uuid,
    workspace_service,
)


class TestContestAccessActions(ContestActionBase):
    def test_contest_writer_manages_others_but_cannot_change_own_role(self) -> None:
        contest_slug, contest_id, _actor_user_id = self.create_contest(
            "writer-access-boundary"
        )
        writer = f"writer-{uuid.uuid4().hex[:8]}"
        target = f"target-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(writer)
        workspace_service.ensure_user(target)
        runtime.contest_service.grant_member_role(contest_id, writer, "write")
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        writer_user_id = workspace_service.known_user_id(writer)
        self.assertIsNotNone(writer_user_id)

        access = runtime.access_query.contest_context(
            contest_id,
            int(writer_user_id),
        )
        self.assertTrue(access["can_write"])
        self.assertTrue(access["can_manage_roster"])
        self.assertTrue(access["can_manage"])

        grant = contest_access_grant(
            contest=contest_slug,
            user=writer,
            target_user=target,
            role="write",
        )
        self.assertEqual(grant.status_code, 303)
        target_membership = runtime.contest_service.membership_for_username(
            contest_id,
            target,
        )
        self.assertIsNotNone(target_membership)
        self.assertEqual(str(target_membership["role"]), "write")
        self.assertIn(
            f"focus_user_id={target_membership['user_id']}",
            str(grant.headers["location"]),
        )
        self.assertTrue(
            str(grant.headers["location"]).endswith("#problem-access-matrix")
        )

        revoke = contest_access_revoke(
            contest=contest_slug,
            user=writer,
            target_user=target,
        )
        self.assertEqual(revoke.status_code, 303)
        self.assertIsNone(
            runtime.contest_service.membership_for_username(contest_id, target)
        )

        self_change = contest_access_grant(
            contest=contest_slug,
            user=writer,
            target_user=writer,
            role="read",
        )
        self.assertEqual(self_change.status_code, 303)
        writer_membership = runtime.contest_service.membership_for_username(
            contest_id,
            writer,
        )
        self.assertIsNotNone(writer_membership)
        self.assertEqual(str(writer_membership["role"]), "write")

    def test_contest_reader_can_exit_but_cannot_revoke_another_member(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "reader-exit"
        )
        _row_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "reader-exit-problem",
        )
        reader = f"reader-{uuid.uuid4().hex[:8]}"
        target = f"target-{uuid.uuid4().hex[:8]}"
        reader_user_id = int(workspace_service.ensure_user(reader)["id"])
        workspace_service.ensure_user(target)
        runtime.contest_service.grant_member_role(contest_id, reader, "read")
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        workspace_service.grant_repo_access(problem_slug, reader, "read")

        with self.assertRaises(HTTPException) as denied:
            contest_access_revoke(
                contest=contest_slug,
                user=reader,
                target_user=target,
            )

        self.assertEqual(denied.exception.status_code, 403)
        self.assertIsNotNone(
            runtime.contest_service.membership_for_username(contest_id, target)
        )

        exited = contest_access_revoke(
            contest=contest_slug,
            user=reader,
            target_user=reader,
        )

        self.assertEqual(exited.status_code, 303)
        self.assertEqual(str(exited.headers["location"]), "/contests")
        self.assertIsNone(
            runtime.contest_service.membership_for_username(contest_id, reader)
        )
        direct_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [problem_id, reader_user_id],
        )
        self.assertIsNotNone(direct_acl)
        self.assertEqual(str(direct_acl["role"]), "read")
        self.assertFalse(
            runtime.access_query.contest_context(
                contest_id,
                reader_user_id,
            )["can_read"]
        )

    def test_problem_access_matrix_saves_direct_acl_atomically(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "matrix-atomic"
        )
        _first_row, first_problem_id, _first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "matrix-first",
        )
        _second_row, second_problem_id, second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "matrix-second",
        )
        target = f"matrix-target-{uuid.uuid4().hex[:8]}"
        target_row = workspace_service.ensure_user(target)
        target_user_id = int(target_row["id"])
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        workspace_service.grant_repo_access(second_slug, target, "read")
        request = _post_form_request(
            f"/contests/{contest_slug}/access/problems/save",
            {
                f"original_role.{first_problem_id}.{target_user_id}": "none",
                f"role.{first_problem_id}.{target_user_id}": "write",
                f"original_role.{second_problem_id}.{target_user_id}": "read",
                f"role.{second_problem_id}.{target_user_id}": "write",
            },
        )

        response = asyncio.run(
            contest_problem_access_save(
                request=request,
                contest=contest_slug,
                user="alice",
            )
        )

        self.assertEqual(response.status_code, 303)
        first_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [first_problem_id, target_user_id],
        )
        self.assertIsNotNone(first_acl)
        self.assertEqual(str(first_acl["role"]), "write")
        second_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [second_problem_id, target_user_id],
        )
        self.assertIsNotNone(second_acl)
        self.assertEqual(str(second_acl["role"]), "write")

    def test_problem_access_matrix_rejects_stale_batch_atomically(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "matrix-stale"
        )
        _first_row, first_problem_id, _first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "matrix-stale-first",
        )
        _second_row, second_problem_id, second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "matrix-stale-second",
        )
        target = f"matrix-stale-target-{uuid.uuid4().hex[:8]}"
        target_user_id = int(workspace_service.ensure_user(target)["id"])
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        workspace_service.grant_repo_access(second_slug, target, "read")
        request = _post_form_request(
            f"/contests/{contest_slug}/access/problems/save",
            {
                f"original_role.{first_problem_id}.{target_user_id}": "none",
                f"role.{first_problem_id}.{target_user_id}": "write",
                f"original_role.{second_problem_id}.{target_user_id}": "none",
                f"role.{second_problem_id}.{target_user_id}": "write",
            },
        )

        response = asyncio.run(
            contest_problem_access_save(
                request=request,
                contest=contest_slug,
                user="alice",
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertIsNone(
            db_fetch_one(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [first_problem_id, target_user_id],
            )
        )
        second_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [second_problem_id, target_user_id],
        )
        self.assertIsNotNone(second_acl)
        self.assertEqual(str(second_acl["role"]), "read")

    def test_problem_access_matrix_rejects_stale_unchanged_cell(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "matrix-stale-unchanged"
        )
        _first_row, first_problem_id, _first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "matrix-stale-unchanged-first",
        )
        _second_row, second_problem_id, second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "matrix-stale-unchanged-second",
        )
        target = f"matrix-stale-unchanged-{uuid.uuid4().hex[:8]}"
        target_user_id = int(workspace_service.ensure_user(target)["id"])
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        workspace_service.grant_repo_access(second_slug, target, "read")
        request = _post_form_request(
            f"/contests/{contest_slug}/access/problems/save",
            {
                f"original_role.{first_problem_id}.{target_user_id}": "none",
                f"role.{first_problem_id}.{target_user_id}": "write",
                f"original_role.{second_problem_id}.{target_user_id}": "none",
                f"role.{second_problem_id}.{target_user_id}": "none",
            },
        )

        response = asyncio.run(
            contest_problem_access_save(
                request=request,
                contest=contest_slug,
                user="alice",
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertIsNone(
            db_fetch_one(
                "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                [first_problem_id, target_user_id],
            )
        )
        second_acl = db_fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [second_problem_id, target_user_id],
        )
        self.assertIsNotNone(second_acl)
        self.assertEqual(str(second_acl["role"]), "read")

    def test_problem_access_matrix_rolls_back_when_actor_cannot_manage_a_row(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "matrix-authorization"
        )
        _first_row, first_problem_id, first_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "matrix-writable",
        )
        _second_row, second_problem_id, _second_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "B",
            "matrix-locked",
        )
        writer = f"matrix-writer-{uuid.uuid4().hex[:8]}"
        target = f"matrix-recipient-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(writer)
        target_row = workspace_service.ensure_user(target)
        target_user_id = int(target_row["id"])
        runtime.contest_service.grant_member_role(contest_id, writer, "write")
        runtime.contest_service.grant_member_role(contest_id, target, "read")
        workspace_service.grant_repo_access(first_slug, writer, "write")
        request = _post_form_request(
            f"/contests/{contest_slug}/access/problems/save",
            {
                f"original_role.{first_problem_id}.{target_user_id}": "none",
                f"role.{first_problem_id}.{target_user_id}": "write",
                f"original_role.{second_problem_id}.{target_user_id}": "none",
                f"role.{second_problem_id}.{target_user_id}": "write",
            },
        )

        with self.assertRaises(HTTPException) as denied:
            asyncio.run(
                contest_problem_access_save(
                    request=request,
                    contest=contest_slug,
                    user=writer,
                )
            )

        self.assertEqual(denied.exception.status_code, 403)
        for problem_id in (first_problem_id, second_problem_id):
            self.assertIsNone(
                db_fetch_one(
                    "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
                    [problem_id, target_user_id],
                )
            )

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
        workspace_service.grant_repo_access(problem_slug, reader, "read")
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

    def test_contest_reader_needs_direct_problem_read_for_review_and_download(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest(
            "reader-problem-boundary"
        )
        self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "reader-problem-boundary",
        )
        reader = f"contest-only-reader-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_user(reader)
        runtime.contest_service.grant_member_role(contest_id, reader, "read")

        with self.assertRaises(HTTPException) as preview_denied:
            contest_statement_review_page(
                _request(f"/contests/{contest_slug}/statements/review"),
                contest_slug,
                reader,
                source="workspace",
                language="english",
            )
        with self.assertRaises(HTTPException) as package_denied:
            contest_packages_download(
                contest=contest_slug,
                user=reader,
                package_format="domjudge",
            )

        self.assertEqual(preview_denied.exception.status_code, 403)
        self.assertEqual(package_denied.exception.status_code, 403)

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

    def test_membership_never_grants_problem_access_or_writes_repo_acl(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("independent-access")
        _row_a, problem_id, _problem_slug = self.add_owned_problem(
            contest_id, actor_user_id, "A", "dynamic-problem"
        )
        workspace_service.ensure_user("bob")
        runtime.contest_service.grant_member_role(contest_id, "bob", "write")
        bob = db_fetch_one("SELECT id FROM users WHERE username='bob'")
        self.assertIsNotNone(bob)
        self.assertFalse(
            runtime.access_query.problem_context(problem_id, int(bob["id"]))["can_read"]
        )
        self.assertNotIn(
            _problem_slug,
            runtime.workspace_service.accessible_problem_slugs(int(bob["id"]), limit=20),
        )
        participating = runtime.workspace_service.participating_problem_rows(
            int(bob["id"]), limit=20
        )
        self.assertNotIn(_problem_slug, [str(row["slug"]) for row in participating])
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
