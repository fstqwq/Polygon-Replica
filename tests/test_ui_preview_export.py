from __future__ import annotations
import re
import zipfile

from tests.ui_support import (
    Path,
    UIBaseSuite,
    _flash_cookie_header,
    _flash_messages_from_response,
    _request,
    _request_with_cookie,
    _wait_for_export_workers,
    _workspace_revision_info,
    artifact_file,
    config,
    db,
    export_create,
    export_import,
    export_import_slug_hint,
    export_page,
    general_page,
    general_save,
    json,
    patch,
    preview_page,
    preview_run,
    preview_status,
    preview_save,
    run_cmd,
    run_page,
    statement_sources_signature,
    threading,
    time,
    uuid,
    workspace_service,
)
from app.impl import auth as auth_impl


class TestUIPreviewExport(UIBaseSuite):
    def _reset_runtime_backend_cache(self) -> None:
        auth_impl._RUNTIME_BACKEND_CACHE = None
        auth_impl._RUNTIME_BACKEND_CACHE_TS = 0.0

    def _ensure_committed_head(self, problem: str, user: str) -> tuple[Path, str]:
        ws = Path(workspace_service.ensure_workspace(problem, user))
        head_res = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"])
        head = head_res.stdout.strip() if head_res.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return ws, head
        marker_rel = f"notes/ui-seed-{uuid.uuid4().hex[:8]}.txt"
        marker = ws / marker_rel
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("seed\n", encoding="utf-8")
        self.assertEqual(run_cmd(["git", "-C", str(ws), "config", "user.name", user]).returncode, 0)
        self.assertEqual(run_cmd(["git", "-C", str(ws), "config", "user.email", f"{user}@polygonlike.local"]).returncode, 0)
        # Seed commit should include the default workspace skeleton to avoid pull conflicts in sibling workspaces.
        self.assertEqual(run_cmd(["git", "-C", str(ws), "add", "-A"]).returncode, 0)
        commit = run_cmd(["git", "-C", str(ws), "commit", "-m", f"ui-seed-{uuid.uuid4().hex[:6]}"])
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_cmd(["git", "-C", str(ws), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)
        workspace_service.ensure_workspace(problem, user, refresh_status=True)
        refreshed_head = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(refreshed_head)
        return ws, refreshed_head

    def _fixture_build_ref(self, build_id: str, problem: str = "alice/sample") -> str:
        return config.fs_manager.compute_build_ref(
            {
                "suite": "ui-preview-export",
                "problem": str(problem or "").strip(),
                "build_id": str(build_id or "").strip(),
            }
        )

    def _fixture_build_root(self, build_id: str, problem: str = "alice/sample") -> tuple[str, Path]:
        build_ref = self._fixture_build_ref(build_id, problem)
        root = config.fs_manager.ensure_build_layout(build_ref).root.resolve()
        return build_ref, root

    def test_preview_page_edits_statement_content_and_problem_title(self) -> None:
        resp = preview_page(_request("/problems/alice/sample/alice/preview"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertNotIn("data-preview-run-form", html)
        self.assertNotIn("data-preview-status-url", html)
        self.assertIn("data-preview-compile-form", html)
        self.assertIn("data-preview-compile-button", html)
        self.assertIn("statement-sections/english/legend.tex", html)
        self.assertIn("statement-sections/english/input.tex", html)
        self.assertIn("statement-sections/english/output.tex", html)
        self.assertIn("statement-sections/english/notes.tex", html)
        self.assertIn("Statement attachments", html)
        self.assertIn(">Upload attachments</a>", html)
        self.assertIn('<h2 class="statement-summary-title">', html)
        self.assertIn('class="statement-summary-meta"', html)
        self.assertIn('class="statement-summary-values"', html)
        self.assertRegex(html, r'class="statement-summary-chip">\s*\d+ms\s*</span>')
        self.assertRegex(html, r'class="statement-summary-chip">\s*\d+MB\s*</span>')
        self.assertRegex(html, r'class="statement-summary-chip">\s*[a-z-]+\s*</span>')
        self.assertIn('data-popup-open="statement-settings-popup"', html)
        self.assertIn('id="statement-settings-popup"', html)
        self.assertIn('action="/problems/alice/sample/alice/statement/save"', html)
        self.assertIn("name=\"problem_name\"", html)
        self.assertNotIn("This title is used in both UI and statement PDF header.", html)
        self.assertIn("name=\"legend_tex\"", html)
        self.assertIn("name=\"input_tex\"", html)
        self.assertIn("name=\"output_tex\"", html)
        self.assertIn("name=\"notes_tex\"", html)
        self.assertRegex(
            html,
            r'<textarea\s+name="legend_tex"[\s\S]*?data-code-height="460"[\s\S]*?data-code-wrap="1"',
        )
        self.assertRegex(
            html,
            r'<textarea\s+name="input_tex"[\s\S]*?data-code-height="230"[\s\S]*?data-code-wrap="1"',
        )
        self.assertRegex(
            html,
            r'<textarea\s+name="output_tex"[\s\S]*?data-code-height="230"[\s\S]*?data-code-wrap="1"',
        )
        self.assertRegex(
            html,
            r'<textarea\s+name="notes_tex"[\s\S]*?data-code-height="460"[\s\S]*?data-code-wrap="1"',
        )
        self.assertNotIn("name=\"interaction_tex\"", html)
        self.assertNotIn("name=\"template_tex\"", html)
        self.assertNotIn("name=\"olmpy_sty\"", html)

        legend_tex = "Saved legend by UI test.\n"
        input_tex = "Saved input by UI test.\n"
        output_tex = "Saved output by UI test.\n"
        notes_tex = "Saved notes by UI test.\n"
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        problem_template_before = (ws / "statement/problem.tex").read_text(encoding="utf-8")
        template_before = (ws / "statement/statements.ftl").read_text(encoding="utf-8")
        style_before = (ws / "statement/olymp.sty").read_text(encoding="utf-8")
        title_resp = general_save(
            problem="alice/sample",
            user="alice",
            problem_name="Preview Saved Title",
        )
        self.assertEqual(title_resp.status_code, 303)
        save_resp = preview_save(
            problem="alice/sample",
            user="alice",
            legend_tex=legend_tex,
            input_tex=input_tex,
            output_tex=output_tex,
            notes_tex=notes_tex,
        )
        self.assertEqual(save_resp.status_code, 303)
        save_messages = _flash_messages_from_response(save_resp)
        self.assertTrue(save_messages)
        self.assertIn("statement saved", save_messages[0])

        self.assertEqual((ws / "statement-sections/english/legend.tex").read_text(encoding="utf-8"), legend_tex)
        self.assertEqual((ws / "statement-sections/english/input.tex").read_text(encoding="utf-8"), input_tex)
        self.assertEqual((ws / "statement-sections/english/output.tex").read_text(encoding="utf-8"), output_tex)
        self.assertEqual((ws / "statement-sections/english/notes.tex").read_text(encoding="utf-8"), notes_tex)
        self.assertFalse((ws / "statement-sections/english/interaction.tex").exists())
        self.assertEqual((ws / "statement/problem.tex").read_text(encoding="utf-8"), problem_template_before)
        self.assertEqual((ws / "statement/statements.ftl").read_text(encoding="utf-8"), template_before)
        self.assertEqual((ws / "statement/olymp.sty").read_text(encoding="utf-8"), style_before)
        problem_row = db.fetch_one("SELECT name FROM problems WHERE slug=?", ["alice/sample"])
        self.assertIsNotNone(problem_row)
        self.assertEqual(str(problem_row["name"]), "Preview Saved Title")
        self.assertFalse((ws / "statement" / "rendered").exists())

        saved_page = preview_page(
            _request_with_cookie("/problems/alice/sample/alice/preview", _flash_cookie_header("statement saved")),
            "alice/sample",
            "alice",
        )
        saved_html = saved_page.body.decode("utf-8", errors="replace")
        self.assertIn('id="top-event-slot"', saved_html)
        self.assertIn('data-top-event="1"', saved_html)
        self.assertIn('data-event-id="', saved_html)
        self.assertIn("statement saved", saved_html)
        self.assertIn("Preview Saved Title", saved_html)
        run_after = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        run_html = run_after.body.decode("utf-8", errors="replace")
        self.assertIn("Preview Saved Title", run_html)

    def test_general_page_without_flash_renders_empty_top_event_slot(self) -> None:
        resp = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('id="top-event-slot"', html)
        self.assertNotIn('data-top-event="1"', html)

    def test_general_page_footer_shows_runtime_profile_info(self) -> None:
        self._reset_runtime_backend_cache()
        resp = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn('id="profile-linux-distro"', html)
        self.assertIn('id="profile-cpu-info"', html)
        self.assertIn('id="profile-sandbox-backend"', html)
        self.assertIn('id="profile-sandbox-backend-count"', html)
        self.assertIn('id="profile-judgehost-backend-summary"', html)
        self.assertRegex(html, r'id="profile-linux-distro"[^>]*>\s*[^<\s].*?</strong>')
        self.assertRegex(html, r'id="profile-cpu-info"[^>]*>\s*[^<\s].*?</strong>')
        self.assertRegex(html, r'id="profile-sandbox-backend"[^>]*>\s*[^<\s].*?</strong>')
        self.assertRegex(html, r'id="profile-sandbox-backend-count"[^>]*>\s*\d+\s*</strong>')
        self.assertIn("Native backend:", html)
        self.assertNotIn(">CPU:</span>", html)
        self.assertRegex(
            html,
            r'id="profile-judgehost-backend-summary"[^>]*>\s*(disabled|online\s+\d+/\d+;\s*queued=\d+;\s*leased=\d+;\s*completed=\d+;\s*failed=\d+)\s*</strong>',
        )

    def test_general_page_footer_judgehost_disabled_hides_queue_details_and_marks_danger(self) -> None:
        self._reset_runtime_backend_cache()
        fake_status = {
            "enabled": False,
            "hosts_online": 0,
            "hosts_total": 2,
            "queue": {"queued": 9, "leased": 8, "completed": 7, "failed": 6},
        }
        with patch.object(config.judgehost_task_service, "status", return_value=fake_status):
            resp = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'id="profile-judgehost-backend-summary"[^>]*class="[^"]*\bdanger\b[^"]*"[^>]*>\s*disabled\s*</strong>',
        )
        self.assertNotIn("queued=", html)
        self.assertNotIn("leased=", html)
        self.assertNotIn("completed=", html)
        self.assertNotIn("failed=", html)

    def test_general_page_footer_judgehost_zero_online_marks_danger_with_detailed_queue(self) -> None:
        self._reset_runtime_backend_cache()
        fake_status = {
            "enabled": True,
            "hosts_online": 0,
            "hosts_total": 2,
            "queue": {"queued": 5, "leased": 4, "completed": 3, "failed": 2},
        }
        with patch.object(config.judgehost_task_service, "status", return_value=fake_status):
            resp = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'id="profile-judgehost-backend-summary"[^>]*class="[^"]*\bdanger\b[^"]*"[^>]*>\s*online\s+0/2;\s*queued=5;\s*leased=4;\s*completed=3;\s*failed=2\s*</strong>',
        )

    def test_general_page_embeds_statement_editor_with_general_return_target(self) -> None:
        resp = general_page(_request("/problems/alice/sample/alice/general"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertNotIn("<h2>Settings</h2>", html)
        self.assertIn("<h2>Statement attachments</h2>", html)
        self.assertIn("<h2>Preview Output</h2>", html)
        self.assertIn('name="page" value="statement"', html)

    def test_preview_page_enables_interaction_section_in_interactive_mode(self) -> None:
        save_general = general_save(problem="alice/sample", user="alice", mode="interactive")
        self.assertEqual(save_general.status_code, 303)
        resp = preview_page(_request("/problems/alice/sample/alice/preview"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("name=\"interaction_tex\"", html)
        self.assertIn("statement-sections/english/interaction.tex", html)

        interaction_tex = "Interactor protocol details.\n"
        save_resp = preview_save(problem="alice/sample", user="alice", interaction_tex=interaction_tex)
        self.assertEqual(save_resp.status_code, 303)
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        self.assertEqual((ws / "statement-sections/english/interaction.tex").read_text(encoding="utf-8"), interaction_tex)

    def test_preview_save_with_problems_target_redirects_to_statement(self) -> None:
        save_resp = preview_save(
            problem="alice/sample",
            user="alice",
            legend_tex="Saved by UI test.",
            page="problems",
        )
        self.assertEqual(save_resp.status_code, 303)
        loc = str(save_resp.headers.get("location", ""))
        self.assertIn("/problems/alice/sample/alice/statement", loc)

    def test_preview_uses_cached_result_without_recompile_on_clean_workspace(self) -> None:
        user = f"cacheuser-{uuid.uuid4().hex[:8]}"
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ws, head = self._ensure_committed_head("alice/sample", user)
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        statement_sig = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))

        cached_id = f"p-cache-{uuid.uuid4().hex[:8]}"
        cached_root = self._artifact_root(cached_id)
        (cached_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (cached_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (cached_root / "logs").mkdir(parents=True, exist_ok=True)
        (cached_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                cached_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                json.dumps({"statement_signature": statement_sig}),
                str(cached_root),
                "2026-02-23T00:04:00Z",
                "2026-02-23T00:04:01Z",
            ],
        )
        stale_id = f"p-cache-stale-{uuid.uuid4().hex[:8]}"
        stale_root = self._artifact_root(stale_id)
        stale_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                stale_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(stale_root),
                "2026-02-23T00:05:00Z",
                "2026-02-23T00:05:01Z",
            ],
        )
        before_count = int(
            db.fetch_one(
                "SELECT COUNT(*) AS c FROM previews WHERE problem_id=? AND workspace_id=?",
                [problem_id, workspace_id],
            )["c"]
        )
        self.assertEqual(before_count, 2)

        run_resp = preview_run("alice/sample", user)
        self.assertEqual(run_resp.status_code, 303)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/alice/sample/{user}/preview", loc)
        self.assertIn(f"preview_id={cached_id}", loc)
        run_messages = _flash_messages_from_response(run_resp)
        self.assertTrue(run_messages)
        self.assertIn("preview compiled", run_messages[0])

        after_count = int(
            db.fetch_one(
                "SELECT COUNT(*) AS c FROM previews WHERE problem_id=? AND workspace_id=?",
                [problem_id, workspace_id],
            )["c"]
        )
        self.assertEqual(after_count, 1)

        page_resp = preview_page(_request(f"/problems/alice/sample/{user}/preview"), "alice/sample", user)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{cached_id}/statement_preview/statement.pdf", page_html)
        self.assertIn("PDF is ready.", page_html)
        self.assertIn("Open in a new tab", page_html)
        self.assertIn("Recompile", page_html)

    def test_preview_run_waits_for_sync_compile_and_redirects_with_preview_id(self) -> None:
        user = f"syncpreview-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        preview_id = f"p-async-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        release_delay_sec = 0.25
        release = threading.Event()

        def _fake_compile_preview(_problem: str, _user: str) -> str:
            release.wait(5.0)
            db.execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    preview_id,
                    problem_id,
                    workspace_id,
                    "",
                    "main",
                    "ok",
                    "{}",
                    str(preview_root),
                    "2026-02-23T00:08:00Z",
                    "2026-02-23T00:08:01Z",
                ],
            )
            return preview_id

        release_thread = threading.Thread(
            target=lambda: (time.sleep(release_delay_sec), release.set()),
            daemon=True,
        )
        release_thread.start()
        with patch.object(config.preview_service, "compile_preview", side_effect=_fake_compile_preview) as compile_mock:
            started_at = time.monotonic()
            run_resp = preview_run("alice/sample", user)
            elapsed = time.monotonic() - started_at
            self.assertGreaterEqual(elapsed, release_delay_sec * 0.8)
            self.assertLess(elapsed, 2.0)

        self.assertEqual(run_resp.status_code, 303)
        compile_mock.assert_called_once_with("alice/sample", user)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/alice/sample/{user}/preview", loc)
        self.assertIn(f"preview_id={preview_id}", loc)
        run_messages = _flash_messages_from_response(run_resp)
        self.assertTrue(run_messages)
        self.assertIn("preview compiled", run_messages[0])
        done_row = db.fetch_one(
            """
            SELECT a.details_json
            FROM audit_log a
            JOIN problems p ON p.id=a.problem_id
            WHERE p.slug=? AND a.action='preview.run'
            ORDER BY a.created_at DESC
            LIMIT 1
            """,
            ["alice/sample"],
        )
        self.assertIsNotNone(done_row)
        done_payload = json.loads(str(done_row["details_json"] or "{}"))
        self.assertEqual(str(done_payload.get("status") or ""), "ok")
        self.assertEqual(str(done_payload.get("preview_id") or ""), preview_id)
        self.assertEqual(str(done_payload.get("source") or ""), "sync")

    def test_preview_status_reports_running_then_ok(self) -> None:
        user = f"statuspreview-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_key = f"{problem_id}:{workspace_id}"

        with config.preview_lock:
            config.preview_inflight.add(workspace_key)
        try:
            while_running = preview_status("alice/sample", user)
            self.assertEqual(while_running.status_code, 200)
            running_payload = json.loads(while_running.body.decode("utf-8"))
            self.assertTrue(bool(running_payload.get("running")))
        finally:
            with config.preview_lock:
                config.preview_inflight.discard(workspace_key)

        preview_id = f"p-status-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(preview_root),
                "2026-02-28T00:08:00Z",
                "2026-02-28T00:08:01Z",
            ],
        )

        after_done = preview_status("alice/sample", user)
        self.assertEqual(after_done.status_code, 200)
        done_payload = json.loads(after_done.body.decode("utf-8"))
        self.assertFalse(bool(done_payload.get("running")))
        self.assertEqual(str(done_payload.get("latest_status") or ""), "ok")
        self.assertEqual(str(done_payload.get("latest_preview_id") or ""), preview_id)

    def test_preview_run_with_contests_target_redirects_to_statement(self) -> None:
        user = f"syncpreview-contests-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-sync-contests-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")

        def _fake_compile_preview(_problem: str, _user: str) -> str:
            db.execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    preview_id,
                    problem_id,
                    workspace_id,
                    "",
                    "main",
                    "ok",
                    "{}",
                    str(preview_root),
                    "2026-02-28T00:08:00Z",
                    "2026-02-28T00:08:01Z",
                ],
            )
            return preview_id

        with patch.object(config.preview_service, "compile_preview", side_effect=_fake_compile_preview) as compile_mock:
            run_resp = preview_run("alice/sample", user, page="contests")
        compile_mock.assert_called_once_with("alice/sample", user)
        self.assertEqual(run_resp.status_code, 303)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/alice/sample/{user}/statement", loc)
        self.assertIn(f"preview_id={preview_id}", loc)

    def test_preview_run_failed_compile_keeps_preview_log_visible(self) -> None:
        user = f"syncpreview-failed-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-sync-failed-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text(
            "statement/main.tex:7 Undefined control sequence\n",
            encoding="utf-8",
        )

        def _fake_compile_preview(_problem: str, _user: str) -> str:
            db.execute(
                """
                INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    preview_id,
                    problem_id,
                    workspace_id,
                    "",
                    "main",
                    "failed",
                    json.dumps({"error": "latex compile failed"}),
                    str(preview_root),
                    "2026-02-28T00:09:00Z",
                    "2026-02-28T00:09:01Z",
                ],
            )
            return preview_id

        with patch.object(config.preview_service, "compile_preview", side_effect=_fake_compile_preview):
            run_resp = preview_run("alice/sample", user)
        self.assertEqual(run_resp.status_code, 303)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/alice/sample/{user}/preview", loc)
        self.assertIn(f"preview_id={preview_id}", loc)
        run_messages = _flash_messages_from_response(run_resp)
        self.assertTrue(run_messages)
        self.assertIn("preview compile failed", run_messages[0].lower())

        page_resp = preview_page(
            _request(f"/problems/alice/sample/{user}/preview", f"preview_id={preview_id}"),
            "alice/sample",
            user,
        )
        html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Compile failed.", html)
        self.assertIn("Open full latex.log", html)
        self.assertIn(f"/artifacts/{preview_id}/logs/latex.log", html)
        self.assertIn("statement/main.tex:7", html)

    def test_preview_page_explicit_failed_preview_without_artifacts_still_shows_error_summary(self) -> None:
        user = f"syncpreview-nolog-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-sync-nolog-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        preview_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "failed",
                json.dumps({"error": "latex compile failed (no log)"}),
                str(preview_root),
                "2026-03-01T00:09:00Z",
                "2026-03-01T00:09:01Z",
            ],
        )

        page_resp = preview_page(
            _request(f"/problems/alice/sample/{user}/preview", f"preview_id={preview_id}"),
            "alice/sample",
            user,
        )
        html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Compile failed.", html)
        self.assertIn("latex compile failed (no log)", html)
        self.assertIn("Latest preview has no PDF output.", html)

    def test_preview_page_compile_summary_prefers_latex_error_line_over_banner(self) -> None:
        user = f"syncpreview-bannersum-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("alice/sample", user)
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-sync-bannersum-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text(
            "\n".join(
                [
                    "This is pdfTeX, Version 3.141592653-2.6-1.40.25",
                    "**main.tex",
                    "(./main.tex",
                    "! LaTeX Error: File `siunitx.sty' not found.",
                    "Type X to quit or <RETURN> to proceed,",
                    "l.15 \\usepackage",
                    ")",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "failed",
                json.dumps({"error": "latex compile failed"}),
                str(preview_root),
                "2026-03-01T00:10:00Z",
                "2026-03-01T00:10:01Z",
            ],
        )

        page_resp = preview_page(
            _request(f"/problems/alice/sample/{user}/preview", f"preview_id={preview_id}"),
            "alice/sample",
            user,
        )
        html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Compile failed.", html)
        self.assertIn("main.tex:15 LaTeX Error: File `siunitx.sty", html)
        self.assertIn("not found.", html)
        self.assertIn("Open full latex.log", html)

    def test_preview_page_auto_selects_valid_cached_preview_without_pruning_stale_rows(self) -> None:
        user = f"cachepage-{uuid.uuid4().hex[:8]}"
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ws, head = self._ensure_committed_head("alice/sample", user)
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        statement_sig = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))

        valid_id = f"p-cache-valid-{uuid.uuid4().hex[:8]}"
        valid_root = self._artifact_root(valid_id)
        (valid_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (valid_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (valid_root / "logs").mkdir(parents=True, exist_ok=True)
        (valid_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                valid_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                json.dumps({"statement_signature": statement_sig}),
                str(valid_root),
                "2026-02-23T00:04:00Z",
                "2026-02-23T00:04:01Z",
            ],
        )
        stale_id = f"p-cache-bad-{uuid.uuid4().hex[:8]}"
        stale_root = self._artifact_root(stale_id)
        stale_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                stale_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(stale_root),
                "2026-02-23T00:06:00Z",
                "2026-02-23T00:06:01Z",
            ],
        )

        resp = preview_page(_request(f"/problems/alice/sample/{user}/preview"), "alice/sample", user)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{valid_id}/statement_preview/statement.pdf", html)
        self.assertRegex(
            html,
            r'data-page="statement"[^>]*>\s*<span class="submenu-title">Statement</span>[\s\S]*?<span class="submenu-status-line(?:\s+)?">statement: ok</span>',
        )
        count = int(
            db.fetch_one(
                "SELECT COUNT(*) AS c FROM previews WHERE problem_id=? AND workspace_id=?",
                [problem_id, workspace_id],
            )["c"]
        )
        self.assertEqual(count, 2)

    def test_preview_cache_invalidates_after_statement_source_change(self) -> None:
        user = f"cacheinv-{uuid.uuid4().hex[:8]}"
        workspace_service.grant_repo_access("alice/sample", user, "owner")
        ws, head = self._ensure_committed_head("alice/sample", user)
        ctx = workspace_service.workspace_context("alice/sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        signature_before = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))
        preview_id = f"p-cache-inv-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                json.dumps({"statement_signature": signature_before}),
                str(preview_root),
                "2026-02-23T00:07:00Z",
                "2026-02-23T00:07:01Z",
            ],
        )

        before_resp = preview_page(_request(f"/problems/alice/sample/{user}/preview"), "alice/sample", user)
        before_html = before_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", before_html)

        content_path = ws / "statement" / "problem.tex"
        content_path.write_text(content_path.read_text(encoding="utf-8") + "\n% cache invalidate\n", encoding="utf-8")
        signature_after = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))
        self.assertNotEqual(signature_before, signature_after)

        after_resp = preview_page(_request(f"/problems/alice/sample/{user}/preview"), "alice/sample", user)
        after_html = after_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", after_html)
        self.assertIn("Compile Statement to generate preview PDF and latex.log.", after_html)

        explicit_resp = preview_page(
            _request(f"/problems/alice/sample/{user}/preview", f"preview_id={preview_id}"),
            "alice/sample",
            user,
        )
        explicit_html = explicit_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", explicit_html)
        self.assertIn("PDF is ready, but statement is stale (sources have changed).", explicit_html)
        self.assertIn("Open in a new tab", explicit_html)
        self.assertRegex(
            explicit_html,
            r'data-page="statement"[^>]*>\s*<span class="submenu-title">Statement</span>\s*<span class="submenu-status submenu-status-stack">\s*<span class="submenu-status-line(?:\s+)?">[^<]+</span>\s*<span class="submenu-status-line submenu-status-warn">statement: stale</span>',
        )

    def test_export_import_polygon_linux_package_updates_workspace_sources(self) -> None:
        class _Upload:
            def __init__(self, path: Path):
                self.filename = path.name
                self.file = path.open("rb")

        package_path = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package_path.exists(), f"missing package fixture: {package_path}")
        upload = _Upload(package_path)
        target_slug = f"imported-{uuid.uuid4().hex[:8]}"
        resp = export_import(problem="alice/sample", user="alice", package_upload=upload, problem_slug=target_slug)
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/problems/alice/{target_slug}/alice/statement", str(resp.headers.get("location", "")))
        flash_messages = _flash_messages_from_response(resp)
        self.assertTrue(flash_messages)
        self.assertIn(f"polygon package imported as alice/{target_slug}", flash_messages[0])

        ws = Path(workspace_service.ensure_workspace(f"alice/{target_slug}", "alice"))
        self.assertTrue((ws / "statement" / "statements.ftl").is_file())
        self.assertTrue((ws / "statement" / "problem.tex").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "legend.tex").is_file())
        problem_row = db.fetch_one("SELECT name FROM problems WHERE slug=?", [f"alice/{target_slug}"])
        self.assertIsNotNone(problem_row)
        self.assertEqual(str(problem_row["name"] or ""), "Guess the Number (Deluxe ver.)")

    def test_export_page_does_not_show_import_entry(self) -> None:
        resp = export_page(_request("/problems/alice/sample/alice/export"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertNotIn('id="polygon-import-form"', html)
        self.assertNotIn('id="polygon-import-slug-hint"', html)
        self.assertNotIn('/problems/alice/sample/alice/export/import/slug-hint', html)

    def test_export_page_shows_running_generation_events(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        db.execute(
            """
            INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at)
            VALUES(?,?,?,?,?)
            """,
            [
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "export.create",
                json.dumps(
                    {
                        "status": "running",
                        "build_id": "b-running-demo",
                        "source_commit": "1234567890abcdef1234567890abcdef12345678",
                        "export_type": "icpc",
                    }
                ),
                "2026-03-03T00:00:00Z",
            ],
        )
        resp = export_page(_request("/problems/alice/sample/alice/export"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Generation Tasks", html)
        self.assertIn("RUNNING", html)
        self.assertIn("b-running-demo", html)
        self.assertIn("/problems/alice/sample/alice/run", html)

    def test_export_import_slug_hint_uses_filename_and_avoids_duplicates(self) -> None:
        token = uuid.uuid4().hex[:8]
        base_slug = f"slug-hint-{token}"
        workspace_service.ensure_problem(f"alice/{base_slug}", f"{base_slug} title")
        resp = export_import_slug_hint("alice/sample", "alice", filename=f"{base_slug}.zip", requested_slug="")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertTrue(bool(payload.get("valid")))
        self.assertTrue(bool(payload.get("exists")))
        self.assertEqual(str(payload.get("base") or ""), base_slug)
        suggested = str(payload.get("suggested") or "")
        self.assertTrue(suggested.startswith(base_slug + "-"))
        self.assertNotEqual(suggested, base_slug)

    def test_export_import_slug_hint_reports_invalid_requested_slug(self) -> None:
        resp = export_import_slug_hint("alice/sample", "alice", filename="abc.zip", requested_slug="Bad_Slug")
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body.decode("utf-8", errors="replace"))
        self.assertTrue(bool(payload.get("ok")))
        self.assertTrue(bool(payload.get("valid")))
        self.assertEqual(str(payload.get("requested_slug") or ""), "bad-slug")
        self.assertEqual(str(payload.get("suggested") or ""), "bad-slug")

    def test_export_import_rejects_duplicate_custom_slug_with_suggestion(self) -> None:
        class _Upload:
            def __init__(self, path: Path):
                self.filename = path.name
                self.file = path.open("rb")

        package_path = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package_path.exists(), f"missing package fixture: {package_path}")
        duplicate_slug = f"import-dup-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(f"alice/{duplicate_slug}", f"{duplicate_slug} title")
        upload = _Upload(package_path)
        resp = export_import(problem="alice/sample", user="alice", package_upload=upload, problem_slug=duplicate_slug)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/problems/alice/sample/alice/export", str(resp.headers.get("location", "")))
        flash_messages = _flash_messages_from_response(resp)
        self.assertTrue(flash_messages)
        self.assertIn(f"problem already exists: alice/{duplicate_slug} (try:", flash_messages[0])

    def test_export_page_shows_revision_used_for_each_package(self) -> None:
        ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-revision-build-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._fixture_build_root(build_id, "alice/sample")
        export_dir = artifact_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = "sample-v1.zip"
        with zipfile.ZipFile(export_dir / stored_filename, "w") as zf:
            root = "sample"
            zf.writestr(f"{root}/problem.yaml", "problem_format_version: 2025-09\n")
            zf.writestr(f"{root}/statement/problem.en.pdf", b"%PDF-1.4\n%%EOF\n")
            zf.writestr(f"{root}/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr(f"{root}/submissions/wrong_answer/wa.cpp", "int main(){return 0;}\n")
            zf.writestr(f"{root}/data/secret/001.in", "1\n")
            zf.writestr(f"{root}/data/secret/002.in", "2\n")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                json.dumps({"steps": [{"step": "validate", "status": "ok"}]}),
                str(artifact_root),
                "2026-02-23T00:12:00Z",
                "2026-02-23T00:12:01Z",
            ],
        )
        export_id = f"ui-export-revision-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,build_ref,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                build_ref,
                workspace_id,
                "icpc",
                stored_filename,
                "b" * 64,
                456,
                head,
                "2026-02-23T00:13:00Z",
            ],
        )

        resp = export_page(_request("/problems/alice/sample/alice/export"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("<th>Summary</th>", html)
        self.assertIn(f"v{int(local_rev)}", html)
        self.assertIn("has pdf", html)
        self.assertIn("validation passed", html)
        self.assertIn("2 solutions (1 correct)", html)
        self.assertIn("2 tests", html)
        self.assertIn(f"sample-v{int(local_rev)}.zip", html)

    def test_export_page_uses_singular_words_for_single_solution_and_test(self) -> None:
        ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-singular-build-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._fixture_build_root(build_id, "alice/sample")
        export_dir = artifact_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = "sample-v1-singular.zip"
        with zipfile.ZipFile(export_dir / stored_filename, "w") as zf:
            root = "sample"
            zf.writestr(f"{root}/problem.yaml", "problem_format_version: 2025-09\n")
            zf.writestr(f"{root}/statement/problem.en.pdf", b"%PDF-1.4\n%%EOF\n")
            zf.writestr(f"{root}/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr(f"{root}/data/secret/001.in", "1\n")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                json.dumps({"steps": [{"step": "validate", "status": "ok"}]}),
                str(artifact_root),
                "2026-02-23T00:16:00Z",
                "2026-02-23T00:16:01Z",
            ],
        )
        export_id = f"ui-export-singular-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,build_ref,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                build_ref,
                workspace_id,
                "icpc",
                stored_filename,
                "d" * 64,
                321,
                head,
                "2026-02-23T00:17:00Z",
            ],
        )

        resp = export_page(_request("/problems/alice/sample/alice/export"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"v{int(local_rev)}", html)
        self.assertIn("1 solution (1 correct)", html)
        self.assertIn("1 test", html)

    def test_export_page_prefers_stored_export_filename(self) -> None:
        ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-display-build-{uuid.uuid4().hex[:8]}"
        build_ref = self._fixture_build_ref(build_id, "alice/sample")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(config.fs_manager.build_paths(build_ref).root.resolve()),
                "2026-02-23T00:14:00Z",
                "2026-02-23T00:14:01Z",
            ],
        )
        export_id = f"ui-export-display-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,build_ref,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                build_ref,
                workspace_id,
                "icpc",
                "stored-name.zip",
                "c" * 64,
                789,
                head,
                "2026-02-23T00:15:00Z",
            ],
        )

        resp = export_page(_request("/problems/alice/sample/alice/export"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("stored-name.zip", html)

    def test_export_download_uses_problem_revision_filename(self) -> None:
        ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-download-build-{uuid.uuid4().hex[:8]}"
        build_ref, artifact_root = self._fixture_build_root(build_id, "alice/sample")
        export_dir = artifact_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = "stored-name.zip"
        (export_dir / stored_filename).write_bytes(b"zip-bytes")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:16:00Z",
                "2026-02-23T00:16:01Z",
            ],
        )
        export_id = f"ui-export-download-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,build_ref,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                build_ref,
                workspace_id,
                "icpc",
                stored_filename,
                "d" * 64,
                999,
                head,
                "2026-02-23T00:17:00Z",
            ],
        )

        resp = artifact_file("alice/sample", "alice", build_id, f"export/{stored_filename}")
        self.assertEqual(resp.status_code, 200)
        cd = str(resp.headers.get("content-disposition", ""))
        self.assertIn(f"sample-v{int(local_rev)}.zip", cd)

    def test_export_create_prefers_committed_head_build(self) -> None:
        _ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        self.assertTrue(head)
        db.execute("DELETE FROM builds WHERE problem_id=? AND workspace_id=?", [problem_id, workspace_id])

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        committed_build_id = f"ui-export-commit-{uuid.uuid4().hex[:8]}"
        dirty_build_ref = self._fixture_build_ref(dirty_build_id, "alice/sample")
        committed_build_ref = self._fixture_build_ref(committed_build_id, "alice/sample")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                dirty_build_ref,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(config.fs_manager.build_paths(dirty_build_ref).root.resolve()),
                "2026-02-23T00:20:00Z",
                "2026-02-23T00:20:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                committed_build_id,
                committed_build_ref,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(config.fs_manager.build_paths(committed_build_ref).root.resolve()),
                "2026-02-23T00:19:00Z",
                "2026-02-23T00:19:01Z",
            ],
        )

        with patch("app.impl.run_export.config.build_service.run_build") as build_mock, patch(
            "app.impl.run_export.config.export_service.create_export",
            return_value=Path("sample-icpc.zip"),
        ) as export_mock:
            resp = export_create(problem="alice/sample", user="alice", build_id="", export_type="icpc")
            _wait_for_export_workers(timeout_sec=10.0)

        self.assertEqual(resp.status_code, 303)
        build_mock.assert_not_called()
        export_mock.assert_called_once()
        args = export_mock.call_args[0]
        self.assertEqual(args[0], "alice/sample")
        self.assertEqual(args[2], "icpc")
        self.assertNotEqual(args[1], dirty_build_id)
        chosen = db.fetch_one(
            "SELECT source_commit,source_ref FROM builds WHERE id=? AND problem_id=? AND workspace_id=?",
            [args[1], problem_id, workspace_id],
        )
        self.assertIsNotNone(chosen)
        self.assertEqual(str(chosen["source_commit"] or "").strip(), head)
        self.assertEqual(str(chosen["source_ref"] or "").strip(), head)

    def test_export_create_builds_committed_head_when_missing(self) -> None:
        _ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        self.assertTrue(head)
        db.execute("DELETE FROM builds WHERE problem_id=? AND workspace_id=?", [problem_id, workspace_id])

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        dirty_build_ref = self._fixture_build_ref(dirty_build_id, "alice/sample")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                dirty_build_ref,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(config.fs_manager.build_paths(dirty_build_ref).root.resolve()),
                "2026-02-23T00:21:00Z",
                "2026-02-23T00:21:01Z",
            ],
        )

        generated_build_id = f"ui-export-generated-{uuid.uuid4().hex[:8]}"
        generated_build_ref = self._fixture_build_ref(generated_build_id, "alice/sample")

        def _fake_run_build(problem: str, user: str, commit: str | None = None, ref: str | None = None) -> str:
            db.execute(
                """
                INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    generated_build_id,
                    generated_build_ref,
                    problem_id,
                    workspace_id,
                    str(commit or ""),
                    str(ref or ""),
                    "ok",
                    "{}",
                    str(config.fs_manager.build_paths(generated_build_ref).root.resolve()),
                    "2026-02-23T00:21:30Z",
                    "2026-02-23T00:21:31Z",
                ],
            )
            return generated_build_id

        with patch("app.impl.run_export.config.build_service.run_build", side_effect=_fake_run_build) as build_mock, patch(
            "app.impl.run_export.config.export_service.create_export",
            return_value=Path("sample-icpc.zip"),
        ) as export_mock:
            resp = export_create(problem="alice/sample", user="alice", build_id="", export_type="icpc")
            _wait_for_export_workers(timeout_sec=10.0)

        self.assertEqual(resp.status_code, 303)
        build_mock.assert_called_once_with("alice/sample", "alice", commit=head, ref=head)
        export_mock.assert_called_once_with("alice/sample", generated_build_id, "icpc")

    def test_export_create_rejects_non_committed_build_id(self) -> None:
        _ws, head = self._ensure_committed_head("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        self.assertTrue(head)

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        dirty_build_ref = self._fixture_build_ref(dirty_build_id, "alice/sample")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                dirty_build_ref,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(config.fs_manager.build_paths(dirty_build_ref).root.resolve()),
                "2026-02-23T00:22:00Z",
                "2026-02-23T00:22:01Z",
            ],
        )

        with patch("app.impl.run_export.config.export_service.create_export") as export_mock:
            resp = export_create(problem="alice/sample", user="alice", build_id=dirty_build_id, export_type="icpc")
            _wait_for_export_workers(timeout_sec=10.0)

        self.assertEqual(resp.status_code, 303)
        export_mock.assert_not_called()
        export_messages = _flash_messages_from_response(resp)
        self.assertTrue(export_messages)
        msg = export_messages[0]
        self.assertIn("queued", msg)
        rows = db.fetch_all(
            "SELECT details_json FROM audit_log WHERE problem_id=? AND action='export.create' ORDER BY id DESC LIMIT 8",
            [problem_id],
        )
        failure_error = ""
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except Exception:
                details = {}
            if str(details.get("status") or "").strip().lower() != "failed":
                continue
            failure_error = str(details.get("error") or "").strip()
            if failure_error:
                break
        self.assertIn("current committed revision", failure_error)

    def test_preview_pdf_artifact_uses_inline_content_disposition(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        preview_id = f"p-ui-inline-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "statement_preview").mkdir(parents=True, exist_ok=True)
        (preview_root / "statement_preview" / "statement.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(preview_root),
                "2026-02-23T00:03:00Z",
                "2026-02-23T00:03:01Z",
            ],
        )
        resp = artifact_file("alice/sample", "alice", preview_id, "statement_preview/statement.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "application/pdf")
        content_disposition = str(resp.headers.get("content-disposition", "")).lower()
        self.assertIn("inline", content_disposition)
        self.assertNotIn("attachment", content_disposition)

    def test_preview_page_sanitizes_latex_log_output(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"])).resolve()
        statement_sig = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))

        preview_id = f"p-ui-logsafe-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        injected = "<script>__LATEX_XSS__</script>"
        log_line = (
            f"{ws.as_posix()}/statement/main.tex:7 Undefined control sequence {injected} "
            "\x1b[31mRED\x1b[0m \u202e\n"
        )
        (preview_root / "logs" / "latex.log").write_text(log_line, encoding="utf-8")
        db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                problem_id,
                workspace_id,
                "",
                "main",
                "failed",
                json.dumps({"statement_signature": statement_sig}),
                str(preview_root),
                "2026-02-23T00:30:00Z",
                "2026-02-23T00:30:01Z",
            ],
        )
        resp = preview_page(_request("/problems/alice/sample/alice/preview", f"preview_id={preview_id}"), "alice/sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("Compile failed.", html)
        self.assertIn("Open full latex.log", html)
        self.assertIn(f"/artifacts/{preview_id}/logs/latex.log", html)
        self.assertIn("statement/main.tex:7", html)
        self.assertNotIn(ws.as_posix(), html)
        self.assertNotIn("<script>__LATEX_XSS__</script>", html)
        self.assertIn("&lt;script&gt;__LATEX_XSS__&lt;/script&gt;", html)
        self.assertNotIn("\x1b[31m", html)

    def test_artifact_log_response_uses_plain_text_and_nosniff(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"b-ui-logsafe-{uuid.uuid4().hex[:8]}"
        build_ref = self._fixture_build_ref(build_id, "alice/sample")
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO builds(id,build_ref,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                build_ref,
                problem_id,
                workspace_id,
                "",
                "main",
                "ok",
                "{}",
                str(artifact_root),
                "2026-02-23T00:31:00Z",
                "2026-02-23T00:31:01Z",
            ],
        )
        resp = artifact_file("alice/sample", "alice", build_id, "logs/latex.log")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(str(resp.media_type).lower().startswith("text/plain"))
        self.assertEqual(str(resp.headers.get("x-content-type-options", "")).lower(), "nosniff")

