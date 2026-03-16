from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.service.platform.process import run_cmd

from .ui_support import (
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _request,
    _wait_for_row,
    contest_access_grant,
    contest_access_page,
    contest_access_revoke,
    contest_overview_page,
    contest_packages_artifact_download,
    contest_packages_build_start,
    contest_packages_job_status,
    contest_packages_page,
    contest_packages_preview_start,
    contest_problems_add,
    contest_problems_change_general,
    contest_problems_page,
    contest_problems_remove_selected,
    contest_problems_renumber,
    contest_problems_reorder,
    contest_properties_page,
    contest_properties_save,
    contests_root_create,
    db,
    git_service,
    json,
    uuid,
    config,
    workspace_service,
)


class TestUIContests(UIBaseSuite):
    def _create_contest(self, slug: str, title: str = "UI Contest") -> int:
        resp = contests_root_create(
            _request("/contests/create"),
            user="alice",
            contest_slug=slug,
            contest_title=title,
        )
        self.assertEqual(resp.status_code, 303)
        row = db.fetch_one("SELECT id FROM contests WHERE slug=?", [slug])
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_contest_create_assigns_owner_membership(self) -> None:
        contest_slug = f"ui-contest-owner-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)
        owner_row = db.fetch_one(
            """
            SELECT role
            FROM contest_members
            WHERE contest_id=?
              AND user_id=(SELECT id FROM users WHERE username='alice')
            """,
            [contest_id],
        )
        self.assertIsNotNone(owner_row)
        self.assertEqual(str(owner_row["role"] or ""), "owner")

    def test_contest_pages_and_problem_management_flow(self) -> None:
        contest_slug = f"ui-contest-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)

        extra_problem = f"alice/ui-contest-prob-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(extra_problem, "Extra Contest Problem")
        workspace_service.grant_repo_access(extra_problem, "alice", "owner")
        workspace_service.grant_repo_access("alice/sample", "alice", "owner")

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=["alice/sample", extra_problem],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        add_msgs = _flash_messages_from_response(add_resp)
        self.assertTrue(add_msgs)
        self.assertIn("added 2 problem", add_msgs[0].lower())

        rows = db.fetch_all(
            "SELECT id,problem_id,idx FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
            [contest_id],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(str(rows[0]["idx"]), "A")
        self.assertEqual(str(rows[1]["idx"]), "B")

        reorder_resp = contest_problems_reorder(
            contest=contest_slug,
            user="alice",
            contest_problem_ids=[str(rows[0]["id"]), str(rows[1]["id"])],
            contest_problem_indices=["B", "A"],
        )
        self.assertEqual(reorder_resp.status_code, 303)
        reordered = db.fetch_all(
            "SELECT id,idx FROM contest_problems WHERE contest_id=? ORDER BY id ASC",
            [contest_id],
        )
        self.assertEqual({str(row["idx"]) for row in reordered}, {"A", "B"})

        renumber_resp = contest_problems_renumber(contest=contest_slug, user="alice")
        self.assertEqual(renumber_resp.status_code, 303)
        renumbered = db.fetch_all(
            "SELECT idx FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
            [contest_id],
        )
        self.assertEqual([str(row["idx"]) for row in renumbered], ["A", "B"])

        remove_resp = contest_problems_remove_selected(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(rows[1]["problem_id"])],
        )
        self.assertEqual(remove_resp.status_code, 303)
        after_remove = db.fetch_all("SELECT problem_id FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(len(after_remove), 1)

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/alice/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Overview", overview_html)
        self.assertIn("Contest Problems", overview_html)

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/alice/problems"),
            contest_slug,
            "alice",
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn("Change Names And TL/ML", problems_html)
        self.assertIn("/problems/change-general", problems_html)

    def test_change_names_tl_ml_creates_per_problem_commit(self) -> None:
        problem_slug = f"alice/ui-bulk-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug, "Bulk Before Name")
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        contest_slug = f"ui-contest-bulk-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Bulk Contest")

        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)
        problem_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        pid = int(problem_row["id"])

        update_resp = contest_problems_change_general(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(pid)],
            problem_ids=[str(pid)],
            problem_names=["Bulk After Name"],
            time_limit_ms_values=["3500"],
            memory_limit_mb_values=["512"],
            retry_job_id="",
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn(f"/contests/{contest_slug}/alice/problems", update_resp.headers.get("location", ""))

        job_row = db.fetch_one(
            "SELECT id,status,summary_json FROM contest_jobs WHERE contest_id=? ORDER BY created_at DESC LIMIT 1",
            [contest_id],
        )
        self.assertIsNotNone(job_row)
        summary = json.loads(str(job_row["summary_json"] or "{}"))
        self.assertEqual(str(summary.get("job_type") or ""), "change-general")
        results = summary.get("results") or []
        self.assertEqual(len(results), 1)
        first = dict(results[0])
        self.assertEqual(str(first.get("status") or ""), "success")
        commit_id = str(first.get("commit_id") or "")
        self.assertRegex(commit_id, r"^[0-9a-f]{40}$")

        updated_problem = db.fetch_one("SELECT name FROM problems WHERE id=?", [pid])
        self.assertIsNotNone(updated_problem)
        self.assertEqual(str(updated_problem["name"]), "Bulk After Name")

        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(int(cfg.get("time_limit_ms") or 0), 3500)
        self.assertEqual(int(cfg.get("memory_limit_mb") or 0), 512)

        last_subject = run_cmd(["git", "-C", str(ws), "log", "-1", "--pretty=%s"]).stdout.strip()
        self.assertEqual(last_subject, f"contest {contest_slug}: bulk update name/TL/ML")

    def test_contest_properties_access_and_packages_pages(self) -> None:
        contest_slug = f"ui-contest-props-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Props Contest")
        workspace_service.ensure_user("bob")

        save_props = contest_properties_save(
            contest=contest_slug,
            user="alice",
            title="Props Contest Updated",
            location="San Francisco",
            date_text="2026-03-01",
            source_mode="built_packages",
        )
        self.assertEqual(save_props.status_code, 303)

        contest_row = db.fetch_one("SELECT title FROM contests WHERE id=?", [contest_id])
        self.assertIsNotNone(contest_row)
        self.assertEqual(str(contest_row["title"]), "Props Contest Updated")

        props_page = contest_properties_page(
            _request(f"/contests/{contest_slug}/alice/properties"),
            contest_slug,
            "alice",
        )
        self.assertEqual(props_page.status_code, 200)
        props_html = props_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Properties", props_html)
        self.assertIn("built_packages", props_html)

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        membership = db.fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNotNone(membership)
        self.assertEqual(str(membership["role"]), "write")

        access_page_resp = contest_access_page(
            _request(f"/contests/{contest_slug}/alice/access"),
            contest_slug,
            "alice",
        )
        self.assertEqual(access_page_resp.status_code, 200)
        access_html = access_page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Access", access_html)
        self.assertIn("bob", access_html)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="bob")
        self.assertEqual(revoke.status_code, 303)
        removed = db.fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(removed)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/alice/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Packages", packages_html)

    def test_contest_preview_and_package_jobs_create_artifacts(self) -> None:
        problem_slug = f"alice/ui-contest-pack-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug, "Contest Package Problem")
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        (ws / "README.problem.md").write_text("contest package test\n", encoding="utf-8")
        commit_id = git_service.commit(ws, "seed commit", "alice", "alice@polygonlike.local")
        git_service.push(ws, "main")
        self.assertRegex(str(commit_id), r"^[0-9a-f]{40}$")

        contest_slug = f"ui-contest-pkg-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Package Build")
        add_resp = contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )
        self.assertEqual(add_resp.status_code, 303)

        def _fake_compile_preview(problem: str, username: str, *, sample_only: bool = False) -> str:
            _ = bool(sample_only)
            problem_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem])
            self.assertIsNotNone(problem_row)
            ws_ctx = workspace_service.workspace_context(problem, username, include_recent=False)
            workspace_id = int(ws_ctx["workspace"]["id"])
            head = str(ws_ctx["workspace"].get("head_commit") or "").strip()
            preview_id = f"p-{uuid.uuid4().hex[:12]}"
            artifact_root = Path(config.settings.artifacts_root) / problem / preview_id
            (artifact_root / "statement_preview").mkdir(parents=True, exist_ok=True)
            (artifact_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%mock preview\n")
            db.execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    preview_id,
                    int(problem_row["id"]),
                    workspace_id,
                    head,
                    head,
                    "ok",
                    json.dumps({"pdf": "statement_preview/statement.pdf"}),
                    str(artifact_root.resolve()),
                    "2026-02-28T00:00:00+00:00",
                    "2026-02-28T00:00:00+00:00",
                ],
            )
            return preview_id

        def _fake_run_build(problem: str, username: str, *, commit: str = "", ref: str = "", force_recompile: bool = False) -> str:
            _ = bool(force_recompile)
            problem_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem])
            self.assertIsNotNone(problem_row)
            ws_ctx = workspace_service.workspace_context(problem, username, include_recent=False)
            workspace_id = int(ws_ctx["workspace"]["id"])
            verification_id = f"ver-{uuid.uuid4().hex[:12]}"
            artifact_root = Path(config.settings.artifacts_root) / problem / verification_id
            artifact_root.mkdir(parents=True, exist_ok=True)
            db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    int(problem_row["id"]),
                    workspace_id,
                    str(commit or "").strip(),
                    str(ref or "").strip(),
                    "verification",
                    "ok",
                    json.dumps({"status": "ok"}),
                    str(artifact_root.resolve()),
                    "2026-02-28T00:00:00+00:00",
                    "2026-02-28T00:00:00+00:00",
                ],
            )
            return verification_id

        def _fake_create_export(problem: str, verification_id: str, export_type: str):
            self.assertEqual(str(export_type), "icpc")
            export_dir = Path(config.settings.artifacts_root) / problem / verification_id / "export"
            export_dir.mkdir(parents=True, exist_ok=True)
            out = export_dir / f"{problem.replace('/', '-')}-v1.zip"
            out.write_bytes(b"PK\x03\x04mock export")
            return out

        with (
            patch.object(config.preview_service, "compile_preview", side_effect=_fake_compile_preview),
            patch.object(config.verification_service, "run_verification", side_effect=_fake_run_build),
            patch.object(config.export_service, "create_export", side_effect=_fake_create_export),
        ):
            preview_start = contest_packages_preview_start(contest=contest_slug, user="alice")
            self.assertEqual(preview_start.status_code, 303)
            preview_q = parse_qs(urlparse(str(preview_start.headers.get("location", ""))).query)
            preview_job_id = str((preview_q.get("job_id") or [""])[0])
            self.assertTrue(preview_job_id)
            preview_done = _wait_for_row(
                "SELECT id,status FROM contest_jobs WHERE id=? AND contest_id=? AND finished_at IS NOT NULL",
                [preview_job_id, contest_id],
            )
            self.assertIsNotNone(preview_done)
            self.assertEqual(str(preview_done["status"]), "ok")

            package_start = contest_packages_build_start(contest=contest_slug, user="alice")
            self.assertEqual(package_start.status_code, 303)
            package_q = parse_qs(urlparse(str(package_start.headers.get("location", ""))).query)
            package_job_id = str((package_q.get("job_id") or [""])[0])
            self.assertTrue(package_job_id)
            package_done = _wait_for_row(
                "SELECT id,status FROM contest_jobs WHERE id=? AND contest_id=? AND finished_at IS NOT NULL",
                [package_job_id, contest_id],
            )
            self.assertIsNotNone(package_done)
            self.assertEqual(str(package_done["status"]), "ok")

        status_resp = contest_packages_job_status(contest=contest_slug, user="alice", job_id=package_job_id)
        self.assertEqual(status_resp.status_code, 200)
        status_payload = json.loads(status_resp.body.decode("utf-8"))
        self.assertEqual(str(status_payload.get("status") or ""), "ok")
        self.assertFalse(bool(status_payload.get("running")))

        preview_artifact = db.fetch_one(
            "SELECT id FROM contest_artifacts WHERE contest_id=? AND job_id=? AND artifact_type='preview-bundle' ORDER BY created_at DESC LIMIT 1",
            [contest_id, preview_job_id],
        )
        self.assertIsNotNone(preview_artifact)
        package_artifact = db.fetch_one(
            "SELECT id FROM contest_artifacts WHERE contest_id=? AND job_id=? AND artifact_type='package-bundle' ORDER BY created_at DESC LIMIT 1",
            [contest_id, package_job_id],
        )
        self.assertIsNotNone(package_artifact)

        download_resp = contest_packages_artifact_download(
            contest=contest_slug,
            user="alice",
            artifact_id=str(package_artifact["id"]),
        )
        self.assertEqual(download_resp.status_code, 200)
        disposition = str(download_resp.headers.get("content-disposition") or "").lower()
        self.assertIn("attachment", disposition)

    def test_contest_status_labels_render_consistently_in_ui(self) -> None:
        contest_slug = f"ui-contest-status-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Status Contest")
        alice_row = db.fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])

        running_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db.execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,summary_json,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                running_job_id,
                contest_id,
                actor_user_id,
                "preview",
                "running",
                json.dumps({"job_type": "preview", "results": []}),
                "2026-03-01T10:00:00+00:00",
                "2026-03-01T10:00:10+00:00",
            ],
        )

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/alice/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn(running_job_id, overview_html)
        self.assertIn("preview", overview_html)
        self.assertIn("RUNNING", overview_html)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/alice/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("<td>RUNNING</td>", packages_html)

        change_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db.execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,summary_json,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [
                change_job_id,
                contest_id,
                actor_user_id,
                "change-general",
                "success",
                json.dumps(
                    {
                        "job_type": "change-general",
                        "results": [
                            {
                                "problem_slug": "alice/sample",
                                "status": "failed",
                                "commit_id": "",
                                "error": "mock error",
                            }
                        ],
                    }
                ),
                "2026-03-01T10:01:00+00:00",
                "2026-03-01T10:01:05+00:00",
            ],
        )

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/alice/problems?job_id={change_job_id}"),
            contest_slug,
            "alice",
            job_id=change_job_id,
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"<code>{change_job_id}</code> / SUCCESS", problems_html)
        self.assertIn("<td>FAILED</td>", problems_html)
