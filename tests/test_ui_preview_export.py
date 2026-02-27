from __future__ import annotations

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
    export_page,
    general_page,
    general_save,
    json,
    patch,
    preview_page,
    preview_run,
    preview_save,
    run_cmd,
    run_page,
    statement_sources_signature,
    uuid,
    workspace_service,
)


class TestUIPreviewExport(UIBaseSuite):
    def test_preview_page_edits_statement_content_and_problem_title(self) -> None:
        resp = preview_page(_request("/problems/sample/alice/preview"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("statement/template.tex", html)
        self.assertIn("statement/content.tex", html)
        self.assertIn("statement/olmpy.sty", html)
        self.assertIn("name=\"problem_name\"", html)
        self.assertNotIn("This title is used in both UI and statement PDF header.", html)
        self.assertIn("name=\"content_tex\"", html)
        self.assertNotIn("name=\"template_tex\"", html)
        self.assertNotIn("name=\"olmpy_sty\"", html)

        content_tex = "\\Section{Problem}\nSaved by UI test.\n"
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        template_before = (ws / "statement/template.tex").read_text(encoding="utf-8")
        style_before = (ws / "statement/olmpy.sty").read_text(encoding="utf-8")
        title_resp = general_save(
            problem="sample",
            user="alice",
            problem_name="Preview Saved Title",
        )
        self.assertEqual(title_resp.status_code, 303)
        save_resp = preview_save(
            problem="sample",
            user="alice",
            content_tex=content_tex,
        )
        self.assertEqual(save_resp.status_code, 303)
        save_messages = _flash_messages_from_response(save_resp)
        self.assertTrue(save_messages)
        self.assertIn("statement saved", save_messages[0])

        self.assertEqual((ws / "statement/content.tex").read_text(encoding="utf-8"), content_tex)
        self.assertEqual((ws / "statement/template.tex").read_text(encoding="utf-8"), template_before)
        self.assertEqual((ws / "statement/olmpy.sty").read_text(encoding="utf-8"), style_before)
        problem_row = db.fetch_one("SELECT name FROM problems WHERE slug=?", ["sample"])
        self.assertIsNotNone(problem_row)
        self.assertEqual(str(problem_row["name"]), "Preview Saved Title")
        rendered_main = (ws / "statement/main.tex").read_text(encoding="utf-8")
        self.assertIn("\\ProblemTitle{Preview Saved Title}", rendered_main)

        saved_page = preview_page(
            _request_with_cookie("/problems/sample/alice/preview", _flash_cookie_header("statement saved")),
            "sample",
            "alice",
        )
        saved_html = saved_page.body.decode("utf-8", errors="replace")
        self.assertIn('<p class="flash flash-floating-center" data-autohide="1">statement saved</p>', saved_html)
        self.assertIn("Preview Saved Title", saved_html)
        run_after = run_page(_request("/problems/sample/alice/run"), "sample", "alice")
        run_html = run_after.body.decode("utf-8", errors="replace")
        self.assertIn("Preview Saved Title", run_html)

    def test_general_page_embeds_statement_editor_with_general_return_target(self) -> None:
        resp = general_page(_request("/problems/sample/alice/general"), "sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("<h2>General Info</h2>", html)
        self.assertIn("<h2>Statement Content</h2>", html)
        self.assertIn("<h2>Preview Output</h2>", html)
        self.assertIn('name="page" value="general"', html)

    def test_preview_save_with_general_target_redirects_back_to_general(self) -> None:
        save_resp = preview_save(
            problem="sample",
            user="alice",
            content_tex="\\Section{Problem}\\nSaved by UI test.\\n",
            page="general",
        )
        self.assertEqual(save_resp.status_code, 303)
        loc = str(save_resp.headers.get("location", ""))
        self.assertIn("/problems/sample/alice/general", loc)

    def test_preview_uses_cached_result_without_recompile_on_clean_workspace(self) -> None:
        user = f"cacheuser-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("sample", user)
        workspace_service.grant_repo_access("sample", user, "owner")
        ctx = workspace_service.workspace_context("sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        head = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head)
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

        run_resp = preview_run("sample", user)
        self.assertEqual(run_resp.status_code, 303)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/sample/{user}/preview", loc)
        self.assertIn(f"preview_id={cached_id}", loc)

        after_count = int(
            db.fetch_one(
                "SELECT COUNT(*) AS c FROM previews WHERE problem_id=? AND workspace_id=?",
                [problem_id, workspace_id],
            )["c"]
        )
        self.assertEqual(after_count, 1)

        page_resp = preview_page(_request(f"/problems/sample/{user}/preview"), "sample", user)
        page_html = page_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{cached_id}/statement_preview/statement.pdf", page_html)
        self.assertIn("PDF is ready.", page_html)
        self.assertIn("Open in a new tab", page_html)
        self.assertIn("Recompile Statement", page_html)

    def test_preview_run_compiles_synchronously_and_redirects_to_preview_id(self) -> None:
        user = f"syncpreview-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("sample", user)
        workspace_service.grant_repo_access("sample", user, "owner")
        ctx = workspace_service.workspace_context("sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        preview_id = f"p-sync-{uuid.uuid4().hex[:8]}"
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
                "2026-02-23T00:08:00Z",
                "2026-02-23T00:08:01Z",
            ],
        )

        with patch.object(config.preview_service, "compile_preview", return_value=preview_id) as compile_mock:
            run_resp = preview_run("sample", user)

        self.assertEqual(run_resp.status_code, 303)
        compile_mock.assert_called_once_with("sample", user)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/sample/{user}/preview", loc)
        self.assertIn(f"preview_id={preview_id}", loc)

    def test_preview_run_with_general_target_redirects_back_to_general(self) -> None:
        user = f"syncpreview-general-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("sample", user)
        workspace_service.grant_repo_access("sample", user, "owner")
        ctx = workspace_service.workspace_context("sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        preview_id = f"p-sync-general-{uuid.uuid4().hex[:8]}"
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
                "2026-02-23T00:08:00Z",
                "2026-02-23T00:08:01Z",
            ],
        )

        with patch.object(config.preview_service, "compile_preview", return_value=preview_id):
            run_resp = preview_run("sample", user, page="general")
        self.assertEqual(run_resp.status_code, 303)
        loc = str(run_resp.headers.get("location", ""))
        self.assertIn(f"/problems/sample/{user}/general", loc)
        self.assertIn(f"preview_id={preview_id}", loc)

    def test_preview_page_auto_selects_valid_cached_preview_and_prunes_stale_rows(self) -> None:
        user = f"cachepage-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("sample", user)
        workspace_service.grant_repo_access("sample", user, "owner")
        ctx = workspace_service.workspace_context("sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        head = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head)
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

        resp = preview_page(_request(f"/problems/sample/{user}/preview"), "sample", user)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{valid_id}/statement_preview/statement.pdf", html)
        self.assertRegex(
            html,
            r'data-page="general"[^>]*>\s*<span class="submenu-title">Statement</span>[\s\S]*?<span class="submenu-status-line(?:\s+)?">statement: ok</span>',
        )
        count = int(
            db.fetch_one(
                "SELECT COUNT(*) AS c FROM previews WHERE problem_id=? AND workspace_id=?",
                [problem_id, workspace_id],
            )["c"]
        )
        self.assertEqual(count, 1)

    def test_preview_cache_invalidates_after_statement_source_change(self) -> None:
        user = f"cacheinv-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_workspace("sample", user)
        workspace_service.grant_repo_access("sample", user, "owner")
        ctx = workspace_service.workspace_context("sample", user, include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        head = run_cmd(["git", "-C", str(ws), "rev-parse", "HEAD"]).stdout.strip()
        self.assertTrue(head)

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

        before_resp = preview_page(_request(f"/problems/sample/{user}/preview"), "sample", user)
        before_html = before_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", before_html)

        content_path = ws / "statement" / "content.tex"
        content_path.write_text(content_path.read_text(encoding="utf-8") + "\n% cache invalidate\n", encoding="utf-8")
        signature_after = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))
        self.assertNotEqual(signature_before, signature_after)

        after_resp = preview_page(_request(f"/problems/sample/{user}/preview"), "sample", user)
        after_html = after_resp.body.decode("utf-8", errors="replace")
        self.assertNotIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", after_html)
        self.assertIn("Compile Statement to generate preview PDF and latex.log.", after_html)

        explicit_resp = preview_page(
            _request(f"/problems/sample/{user}/preview", f"preview_id={preview_id}"),
            "sample",
            user,
        )
        explicit_html = explicit_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/artifacts/{preview_id}/statement_preview/statement.pdf", explicit_html)
        self.assertIn("PDF is ready, but statement is stale (sources have changed).", explicit_html)
        self.assertIn("Open in a new tab", explicit_html)
        self.assertRegex(
            explicit_html,
            r'data-page="general"[^>]*>\s*<span class="submenu-title">Statement</span>\s*<span class="submenu-status submenu-status-stack">\s*<span class="submenu-status-line(?:\s+)?">[^<]+</span>\s*<span class="submenu-status-line submenu-status-warn">statement: stale</span>',
        )

    def test_export_page_shows_revision_used_for_each_package(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        ws = Path(str(ctx["workspace"]["path"]))
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-revision-build-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(self._artifact_root(build_id)),
                "2026-02-23T00:12:00Z",
                "2026-02-23T00:12:01Z",
            ],
        )
        export_id = f"ui-export-revision-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                workspace_id,
                "icpc",
                "sample-v1.zip",
                "b" * 64,
                456,
                head,
                "2026-02-23T00:13:00Z",
            ],
        )

        resp = export_page(_request("/problems/sample/alice/export"), "sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("<th>Revision</th>", html)
        self.assertIn(f"v{int(local_rev)}", html)
        self.assertIn(f"sample-v{int(local_rev)}.zip", html)

    def test_export_page_uses_problem_revision_display_filename(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        ws = Path(str(ctx["workspace"]["path"]))
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-display-build-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(self._artifact_root(build_id)),
                "2026-02-23T00:14:00Z",
                "2026-02-23T00:14:01Z",
            ],
        )
        export_id = f"ui-export-display-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO exports(id,problem_id,build_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                workspace_id,
                "icpc",
                "stored-name.zip",
                "c" * 64,
                789,
                head,
                "2026-02-23T00:15:00Z",
            ],
        )

        resp = export_page(_request("/problems/sample/alice/export"), "sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"sample-v{int(local_rev)}.zip", html)

    def test_export_download_uses_problem_revision_filename(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        ws = Path(str(ctx["workspace"]["path"]))
        revision_info = _workspace_revision_info(ws, "main", fetch_remote=False)
        local_rev = revision_info.get("local")
        self.assertIsInstance(local_rev, int)
        self.assertGreaterEqual(int(local_rev), 1)

        build_id = f"ui-export-download-build-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        export_dir = artifact_root / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        stored_filename = "stored-name.zip"
        (export_dir / stored_filename).write_bytes(b"zip-bytes")
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
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
            INSERT INTO exports(id,problem_id,build_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                problem_id,
                build_id,
                workspace_id,
                "icpc",
                stored_filename,
                "d" * 64,
                999,
                head,
                "2026-02-23T00:17:00Z",
            ],
        )

        resp = artifact_file("sample", "alice", build_id, f"export/{stored_filename}")
        self.assertEqual(resp.status_code, 200)
        cd = str(resp.headers.get("content-disposition", ""))
        self.assertIn(f"sample-v{int(local_rev)}.zip", cd)

    def test_export_create_prefers_committed_head_build(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        self.assertTrue(head)
        db.execute("DELETE FROM builds WHERE problem_id=? AND workspace_id=?", [problem_id, workspace_id])

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        committed_build_id = f"ui-export-commit-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(self._artifact_root(dirty_build_id)),
                "2026-02-23T00:20:00Z",
                "2026-02-23T00:20:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                committed_build_id,
                problem_id,
                workspace_id,
                head,
                head,
                "ok",
                "{}",
                str(self._artifact_root(committed_build_id)),
                "2026-02-23T00:19:00Z",
                "2026-02-23T00:19:01Z",
            ],
        )

        with patch("app.impl.run_export.config.build_service.run_build") as build_mock, patch(
            "app.impl.run_export.config.export_service.create_export",
            return_value=Path("sample-icpc.zip"),
        ) as export_mock:
            resp = export_create(problem="sample", user="alice", build_id="", export_type="icpc")
            _wait_for_export_workers(timeout_sec=10.0)

        self.assertEqual(resp.status_code, 303)
        build_mock.assert_not_called()
        export_mock.assert_called_once()
        args = export_mock.call_args[0]
        self.assertEqual(args[0], "sample")
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
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        self.assertTrue(head)
        db.execute("DELETE FROM builds WHERE problem_id=? AND workspace_id=?", [problem_id, workspace_id])

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(self._artifact_root(dirty_build_id)),
                "2026-02-23T00:21:00Z",
                "2026-02-23T00:21:01Z",
            ],
        )

        generated_build_id = f"ui-export-generated-{uuid.uuid4().hex[:8]}"

        def _fake_run_build(problem: str, user: str, commit: str | None = None, ref: str | None = None) -> str:
            db.execute(
                """
                INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    generated_build_id,
                    problem_id,
                    workspace_id,
                    str(commit or ""),
                    str(ref or ""),
                    "ok",
                    "{}",
                    str(self._artifact_root(generated_build_id)),
                    "2026-02-23T00:21:30Z",
                    "2026-02-23T00:21:31Z",
                ],
            )
            return generated_build_id

        with patch("app.impl.run_export.config.build_service.run_build", side_effect=_fake_run_build) as build_mock, patch(
            "app.impl.run_export.config.export_service.create_export",
            return_value=Path("sample-icpc.zip"),
        ) as export_mock:
            resp = export_create(problem="sample", user="alice", build_id="", export_type="icpc")
            _wait_for_export_workers(timeout_sec=10.0)

        self.assertEqual(resp.status_code, 303)
        build_mock.assert_called_once_with("sample", "alice", commit=head, ref=head)
        export_mock.assert_called_once_with("sample", generated_build_id, "icpc")

    def test_export_create_rejects_non_committed_build_id(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        head = str(ctx["workspace"].get("head_commit") or "").strip()
        self.assertTrue(head)

        dirty_build_id = f"ui-export-dirty-{uuid.uuid4().hex[:8]}"
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                dirty_build_id,
                problem_id,
                workspace_id,
                head,
                "main",
                "ok",
                "{}",
                str(self._artifact_root(dirty_build_id)),
                "2026-02-23T00:22:00Z",
                "2026-02-23T00:22:01Z",
            ],
        )

        with patch("app.impl.run_export.config.export_service.create_export") as export_mock:
            resp = export_create(problem="sample", user="alice", build_id=dirty_build_id, export_type="icpc")
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
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
        resp = artifact_file("sample", "alice", preview_id, "statement_preview/statement.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "application/pdf")
        content_disposition = str(resp.headers.get("content-disposition", "")).lower()
        self.assertIn("inline", content_disposition)
        self.assertNotIn("attachment", content_disposition)

    def test_preview_page_sanitizes_latex_log_output(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
        resp = preview_page(_request("/problems/sample/alice/preview", f"preview_id={preview_id}"), "sample", "alice")
        html = resp.body.decode("utf-8", errors="replace")
        self.assertIn("statement/main.tex:7", html)
        self.assertNotIn(ws.as_posix(), html)
        self.assertNotIn("<script>__LATEX_XSS__</script>", html)
        self.assertIn("&lt;script&gt;__LATEX_XSS__&lt;/script&gt;", html)
        self.assertNotIn("\x1b[31m", html)

    def test_artifact_log_response_uses_plain_text_and_nosniff(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"b-ui-logsafe-{uuid.uuid4().hex[:8]}"
        artifact_root = self._artifact_root(build_id)
        (artifact_root / "logs").mkdir(parents=True, exist_ok=True)
        (artifact_root / "logs" / "latex.log").write_text("ok\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
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
        resp = artifact_file("sample", "alice", build_id, "logs/latex.log")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(str(resp.media_type).lower().startswith("text/plain"))
        self.assertEqual(str(resp.headers.get("x-content-type-options", "")).lower(), "nosniff")
