from __future__ import annotations

from .db_helpers import (
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
    write_contest_job_summary,
)

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.service.platform.git_process import run_git
from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.sandbox.base import ExecResult
from app.service.statement.render import ensure_statement_language_sources
from app.service.verification.signature import verification_signature

from .ui_support import (
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _register_with_password_proof,
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
        row = db_fetch_one("SELECT id FROM contests WHERE slug=?", [slug])
        self.assertIsNotNone(row)
        return int(row["id"])

    def test_contest_create_assigns_owner_membership(self) -> None:
        contest_slug = f"ui-contest-owner-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug)
        owner_row = db_fetch_one(
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
        workspace_service.ensure_problem(extra_problem)
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

        rows = db_fetch_all(
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
        reordered = db_fetch_all(
            "SELECT id,idx FROM contest_problems WHERE contest_id=? ORDER BY id ASC",
            [contest_id],
        )
        self.assertEqual({str(row["idx"]) for row in reordered}, {"A", "B"})

        renumber_resp = contest_problems_renumber(contest=contest_slug, user="alice")
        self.assertEqual(renumber_resp.status_code, 303)
        renumbered = db_fetch_all(
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
        after_remove = db_fetch_all("SELECT problem_id FROM contest_problems WHERE contest_id=?", [contest_id])
        self.assertEqual(len(after_remove), 1)

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Overview", overview_html)
        self.assertIn("Contest Problems", overview_html)

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/problems"),
            contest_slug,
            "alice",
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn("Change TL/ML", problems_html)
        self.assertIn("/problems/change-general", problems_html)

    def test_change_names_tl_ml_creates_per_problem_commit(self) -> None:
        problem_slug = f"alice/ui-bulk-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
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
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        pid = int(problem_row["id"])

        update_resp = contest_problems_change_general(
            contest=contest_slug,
            user="alice",
            selected_problem_ids=[str(pid)],
            problem_ids=[str(pid)],
            time_limit_ms_values=["3500"],
            memory_limit_mb_values=["512"],
            retry_job_id="",
        )
        self.assertEqual(update_resp.status_code, 303)
        self.assertIn(f"/contests/{contest_slug}/problems", update_resp.headers.get("location", ""))

        job_row = db_fetch_one(
            "SELECT id,status FROM contest_jobs WHERE contest_id=? ORDER BY created_at DESC LIMIT 1",
            [contest_id],
        )
        self.assertIsNotNone(job_row)
        summary = read_contest_job_summary(contest_id, str(job_row["id"]))
        self.assertEqual(str(summary.get("job_type") or ""), "change-general")
        results = summary.get("results") or []
        self.assertEqual(len(results), 1)
        first = dict(results[0])
        self.assertEqual(str(first.get("status") or ""), "success")
        commit_id = str(first.get("commit_id") or "")
        self.assertRegex(commit_id, r"^[0-9a-f]{40}$")

        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(int(cfg.get("time_limit_ms") or 0), 3500)
        self.assertEqual(int(cfg.get("memory_limit_mb") or 0), 512)

        last_subject = run_git(["git", "-C", str(ws), "log", "-1", "--pretty=%s"]).stdout.strip()
        self.assertEqual(last_subject, f"contest {contest_slug}: bulk update TL/ML")

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
        )
        self.assertEqual(save_props.status_code, 303)
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        config.contest_service.set_statement_default_language(contest_id, int(alice_row["id"]), "english")

        contest_row = db_fetch_one("SELECT title FROM contests WHERE id=?", [contest_id])
        self.assertIsNotNone(contest_row)
        self.assertEqual(str(contest_row["title"]), "Props Contest Updated")

        props_page = contest_properties_page(
            _request(f"/contests/{contest_slug}/properties"),
            contest_slug,
            "alice",
        )
        self.assertEqual(props_page.status_code, 200)
        props_html = props_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Properties", props_html)
        self.assertIn("Statement Language", props_html)
        self.assertIn("english", props_html)

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="write")
        self.assertEqual(grant.status_code, 303)
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNotNone(membership)
        self.assertEqual(str(membership["role"]), "write")

        access_page_resp = contest_access_page(
            _request(f"/contests/{contest_slug}/access"),
            contest_slug,
            "alice",
        )
        self.assertEqual(access_page_resp.status_code, 200)
        access_html = access_page_resp.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Access", access_html)
        self.assertIn("bob", access_html)
        self.assertIn('option value="write"', access_html)
        self.assertIn('option value="read"', access_html)
        self.assertNotIn('option value="owner"', access_html)
        self.assertIn("fixed owner", access_html)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="bob")
        self.assertEqual(revoke.status_code, 303)
        removed = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(removed)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Contest Packages", packages_html)

    def test_contest_overview_best_effort_infers_location_and_date_from_statements(self) -> None:
        contest_slug = f"ui-contest-overview-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Overview Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": (
                        b"\\documentclass{article}\n"
                        b"\\begin{document}\n"
                        b"\\contest\n"
                        b"{Overview Contest}%\n"
                        b"{Hangzhou, China}%\n"
                        b"{1 February, 2026}%\n"
                        b"\\end{document}\n"
                    ),
                }
            ],
        )
        config.contest_service.set_statement_default_language(contest_id, actor_user_id, "english")

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn("Hangzhou, China", overview_html)
        self.assertIn("1 February, 2026", overview_html)

    def test_contest_access_cannot_transfer_owner_role(self) -> None:
        contest_slug = f"ui-contest-owner-transfer-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Owner Transfer Contest")
        _register_with_password_proof("bob", "StrongPass123", next_path="/")

        grant = contest_access_grant(contest=contest_slug, user="alice", target_user="bob", role="owner")
        self.assertEqual(grant.status_code, 303)
        grant_messages = _flash_messages_from_response(grant)
        self.assertTrue(grant_messages)
        self.assertIn("owner access is fixed and cannot be transferred", grant_messages[0])
        membership = db_fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=(SELECT id FROM users WHERE username='bob')",
            [contest_id],
        )
        self.assertIsNone(membership)

        revoke = contest_access_revoke(contest=contest_slug, user="alice", target_user="alice")
        self.assertEqual(revoke.status_code, 303)
        revoke_messages = _flash_messages_from_response(revoke)
        self.assertTrue(revoke_messages)
        self.assertIn("owner access is fixed and cannot be transferred", revoke_messages[0])

    def test_contest_pdf_and_package_jobs_create_artifacts(self) -> None:
        problem_slug = f"alice/ui-contest-pack-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "README.problem.md").write_text("contest package test\n", encoding="utf-8")
        (ws / "statement" / "olymp.sty").write_text("% problem style\n", encoding="utf-8")
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Problem legend\n", encoding="utf-8")
        (ws / "statement-assets").mkdir(parents=True, exist_ok=True)
        (ws / "statement-assets" / "example.mp").write_text("verbatimtex\netex\nbeginfig(1);endfig;end.\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("1 2 3\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"version": 2, "tests": [{"id": "001", "kind": "manual", "sample": True}]}, indent=2) + "\n",
            encoding="utf-8",
        )
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
        problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        problem_id = int(problem_row["id"])
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": b"\\\\documentclass{article}\n\\\\usepackage{olymp}\n\\\\begin{document}\n\\\\import{../../problems/src-problem/statements/}{./problem.tex}\n\\\\end{document}\n",
                },
                {
                    "key": "statements/english/olymp.sty",
                    "language": "english",
                    "package_bytes": b"% contest style\n",
                },
                {
                    "key": "statements/english/banner.tex",
                    "language": "english",
                    "package_bytes": b"% contest banner\n",
                },
                {
                    "key": "statements/english/banner.png",
                    "language": "english",
                    "package_bytes": b"\x89PNG\r\n\x1a\nmock",
                },
            ],
        )
        config.contest_service.set_statement_default_language(contest_id, actor_user_id, "english")
        config.contest_service.set_statement_problem_source_folders(contest_id, actor_user_id, {problem_id: "src-problem"})

        tex_commands: list[tuple[list[str], str, tuple[str, ...], dict[str, str] | None]] = []

        def _fake_sandbox_run(spec):
            command = [str(token) for token in spec.command]
            cwd = Path(spec.cwd)
            tex_commands.append(
                (
                    command,
                    str(cwd),
                    tuple(str(Path(path)) for path in spec.extra_mounts),
                    None if spec.env is None else dict(spec.env),
                )
            )
            if command[0] == "extractbb":
                source = cwd / command[1]
                (source.with_suffix(source.suffix + ".xbb")).write_text("%%BoundingBox: 0 0 10 10\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "mpost":
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "latex":
                (cwd / "statements.dvi").write_bytes(b"DVI")
                (cwd / "statements.log").write_text("latex ok\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "dvips":
                (cwd / "statements.ps").write_bytes(b"PS")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            if command[0] == "dvipdfmx":
                (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n%mock contest pdf\n")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            return ExecResult(backend="test", status="error", returncode=1, elapsed_ms=1, stdout="", stderr="unexpected command")

        def _fake_run_build(problem: str, username: str, *, commit: str = "", ref: str = "", force_recompile: bool = False) -> str:
            _ = bool(force_recompile)
            problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem])
            self.assertIsNotNone(problem_row)
            ws_ctx = workspace_service.workspace_context(problem, username, include_recent=False)
            workspace_id = int(ws_ctx["workspace"]["id"])
            workspace_path = Path(str(ws_ctx["workspace"]["path"] or "")).resolve()
            verification_id = f"ver-{uuid.uuid4().hex[:12]}"
            artifact_root = config.fs_manager.prepare_verification_layout(verification_id).root
            artifact_root.mkdir(parents=True, exist_ok=True)
            db_execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    int(problem_row["id"]),
                    workspace_id,
                    verification_signature(workspace_path),
                    "all",
                    "ok",
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

        sample_sync_calls: list[tuple[str, str, str]] = []

        def _fake_sync_sample_payloads(problem: str, username: str, snapshot: Path) -> dict[str, object]:
            sample_sync_calls.append((problem, username, str(snapshot)))
            tests = load_tests_spec(snapshot / "tests" / "spec.json")
            tests[0]["sample_output"] = "6\n"
            (snapshot / "tests" / "spec.json").write_text(dumps_tests_spec(tests), encoding="utf-8")
            return {"sample_count": 1, "copied": 1, "verification_id": "ver-sample-sync"}

        with (
            patch.object(config.tex_compile_service.sandbox, "run", side_effect=_fake_sandbox_run),
            patch.object(config.preview_service, "sync_sample_payloads_for_snapshot", side_effect=_fake_sync_sample_payloads),
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

        preview_artifact = db_fetch_one(
            "SELECT id FROM contest_artifacts WHERE contest_id=? AND job_id=? AND artifact_type='contest-pdf' ORDER BY created_at DESC LIMIT 1",
            [contest_id, preview_job_id],
        )
        self.assertIsNotNone(preview_artifact)
        package_artifact = db_fetch_one(
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
        preview_job = db_fetch_one(
            "SELECT id FROM contest_jobs WHERE id=? AND contest_id=?",
            [preview_job_id, contest_id],
        )
        self.assertIsNotNone(preview_job)
        preview_summary = read_contest_job_summary(contest_id, preview_job_id)
        self.assertEqual(str(preview_summary.get("job_type") or ""), "pdf")
        self.assertEqual(str(preview_summary.get("language") or ""), "english")
        self.assertTrue(str(preview_summary.get("pdf_file") or "").endswith("statements.pdf"))
        contest_job_root = config.contest_service.job_root(contest_slug, preview_job_id)
        compile_root = contest_job_root / "contest-pdf-src"
        self.assertTrue((compile_root / "statements" / "english" / "olymp.sty").is_file())
        self.assertEqual((compile_root / "statements" / "english" / "olymp.sty").read_text(encoding="utf-8"), "% contest style\n")
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").is_file())
        rendered_problem_tex = (compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\Example", rendered_problem_tex)
        self.assertIn("sample.001.in", rendered_problem_tex)
        self.assertIn("sample.001.ans", rendered_problem_tex)
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.in").is_file())
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.ans").is_file())
        self.assertEqual(len(sample_sync_calls), 1)
        self.assertEqual(sample_sync_calls[0][0], problem_slug)
        self.assertEqual(sample_sync_calls[0][1], "alice")
        command_names = [command[0] for command, _cwd, _mounts, _env in tex_commands]
        self.assertIn("extractbb", command_names)
        self.assertIn("mpost", command_names)
        self.assertIn("latex", command_names)
        self.assertIn("dvips", command_names)
        self.assertIn("dvipdfmx", command_names)
        self.assertNotIn("pdflatex", command_names)
        self.assertFalse((compile_root / "statements" / "english" / "tutorials.pdf").exists())
        for _command, _cwd, mounts, env in tex_commands:
            self.assertIn(str(compile_root), mounts)
            self.assertIsNotNone(env)
            assert env is not None
            self.assertEqual(env.get("HOME"), str(compile_root))
            self.assertEqual(env.get("TEXMFVAR"), str(compile_root / ".texmf-var"))
            self.assertEqual(env.get("TEXMFCACHE"), str(compile_root / ".texmf-cache"))
            self.assertEqual(env.get("TEXMFCONFIG"), str(compile_root / ".texmf-config"))
            self.assertEqual(env.get("VARTEXFONTS"), str(compile_root / ".texfonts"))
            self.assertEqual(env.get("TEXMFOUTPUT"), str(compile_root / ".texmf-output"))
        latex_commands = [command for command, _cwd, _mounts, _env in tex_commands if command[0] == "latex"]
        self.assertEqual(len(latex_commands), 2)
        for command in latex_commands:
            self.assertIn("-interaction=nonstopmode", command)
            self.assertIn("-halt-on-error", command)
            self.assertIn("-jobname=statements", command)
            self.assertEqual(command[-1], "__contest_wrapper__.tex")
        wrapper_path = compile_root / "statements" / "english" / "__contest_wrapper__.tex"
        self.assertTrue(wrapper_path.is_file())
        wrapper_text = wrapper_path.read_text(encoding="utf-8")
        self.assertIn("\\AtBeginDocument", wrapper_text)
        self.assertIn("\\providecommand{\\url}[1]", wrapper_text)
        self.assertIn("\\providecommand{\\href}[2]", wrapper_text)

    def test_contest_statement_sources_normalize_text_newlines(self) -> None:
        contest_slug = f"ui-contest-src-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Source Normalize")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])
        config.contest_service.replace_statement_sources(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=actor_user_id,
            files=[
                {
                    "key": "statements/english/statements.tex",
                    "language": "english",
                    "package_bytes": b"\\documentclass{article}\r\n\\begin{document}\r\ncontest\r\n\\end{document}\r\n",
                },
                {
                    "key": "statements/english/banner.png",
                    "language": "english",
                    "package_bytes": b"\x89PNG\r\n\x1a\nmock",
                },
            ],
        )
        text_path = config.contest_service.statement_file_path(contest_slug, "statements/english/statements.tex")
        self.assertEqual(text_path.read_text(encoding="utf-8"), "\\documentclass{article}\n\\begin{document}\ncontest\n\\end{document}\n")
        image_path = config.contest_service.statement_file_path(contest_slug, "statements/english/banner.png")
        self.assertEqual(image_path.read_bytes(), b"\x89PNG\r\n\x1a\nmock")

    def test_contest_status_labels_render_consistently_in_ui(self) -> None:
        contest_slug = f"ui-contest-status-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Status Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        actor_user_id = int(alice_row["id"])

        running_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                running_job_id,
                contest_id,
                actor_user_id,
                "pdf",
                "running",
                "2026-03-01T10:00:00+00:00",
                "2026-03-01T10:00:10+00:00",
            ],
        )
        write_contest_job_summary(contest_id, running_job_id, {"job_type": "pdf", "results": [], "language": "english"})

        overview = contest_overview_page(
            _request(f"/contests/{contest_slug}/overview"),
            contest_slug,
            "alice",
        )
        self.assertEqual(overview.status_code, 200)
        overview_html = overview.body.decode("utf-8", errors="replace")
        self.assertIn(running_job_id, overview_html)
        self.assertIn("pdf", overview_html)
        self.assertIn("RUNNING", overview_html)

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("<td>RUNNING</td>", packages_html)

        change_job_id = f"cj-{uuid.uuid4().hex[:10]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                change_job_id,
                contest_id,
                actor_user_id,
                "change-general",
                "success",
                "2026-03-01T10:01:00+00:00",
                "2026-03-01T10:01:05+00:00",
            ],
        )
        write_contest_job_summary(
            contest_id,
            change_job_id,
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
            },
        )

        problems_page = contest_problems_page(
            _request(f"/contests/{contest_slug}/problems?job_id={change_job_id}"),
            contest_slug,
            "alice",
            job_id=change_job_id,
        )
        self.assertEqual(problems_page.status_code, 200)
        problems_html = problems_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"<code>{change_job_id}</code> / SUCCESS", problems_html)
        self.assertIn("<td>FAILED</td>", problems_html)
