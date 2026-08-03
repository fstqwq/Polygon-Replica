from __future__ import annotations

import asyncio
import io

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.sandbox.base import ExecResult
from app.service.statement.render import ensure_statement_language_sources

from .contest_support import ContestActionBase
from .db_helpers import db_execute, db_fetch_one, read_contest_job_summary, write_contest_job_summary
from .ui_support import (
    Path,
    _request,
    _wait_for_row,
    config,
    contest_packages_artifact_download,
    contest_packages_build_start,
    contest_packages_job_status,
    contest_packages_page,
    contest_packages_preview_start,
    contest_problems_add,
    contest_statement_source_file,
    contest_statement_source_save,
    contest_statement_source_upload,
    git_service,
    json,
    uuid,
    workspace_service,
)


class TestContestBuilds(ContestActionBase):
    class _FakeUpload:
        def __init__(self, filename: str, data: bytes):
            self.filename = filename
            self._buf = io.BytesIO(data)

        async def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        async def close(self) -> None:
            self._buf.close()

    def test_commit_verification_records_resolved_source_commit(self) -> None:
        problem_slug = f"alice/commit-verification-{self.random_id('problem')}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        workspace = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        (workspace / "README.problem.md").write_text("committed source\n", encoding="utf-8")
        committed = git_service.commit(workspace, "seed verification source", "alice", "alice@polygonlike.local")
        git_service.push(workspace, "main")
        workspace_service.ensure_workspace(problem_slug, "alice", refresh_status=True)
        workspace_ctx = workspace_service.workspace_context(problem_slug, "alice", include_recent=False)
        head_commit = str(workspace_ctx["workspace"]["head_commit"])
        self.assertEqual(head_commit, committed)

        with patch("app.impl.workspace.verification_dag.run_workspace_verification_dag") as run_dag:
            config.verification_service.run_verification(problem_slug, "alice", commit=head_commit)

        commit_calls = [
            call
            for call in run_dag.call_args_list
            if call.args[0] == problem_slug and call.kwargs.get("source_commit") == head_commit
        ]
        self.assertEqual(len(commit_calls), 1)
        self.assertEqual(commit_calls[0].kwargs["workspace_head"], head_commit)
        self.assertFalse(commit_calls[0].kwargs["workspace_dirty"])

    def test_statements_and_builds_contract_and_common_error(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("builds")
        _contest_problem_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "build-problem",
        )
        common_error = "shared package failure"
        job_id = config.contest_service.create_job(
            contest_id,
            actor_user_id,
            "package",
            "failed",
            {
                "job_type": "package",
                "common_error": common_error,
                "results": [
                    {
                        "idx": "A",
                        "problem_id": problem_id,
                        "problem_slug": problem_slug,
                        "status": "failed",
                        "source_commit": "a" * 40,
                        "error": common_error,
                    },
                    {
                        "idx": "B",
                        "problem_id": problem_id,
                        "problem_slug": problem_slug,
                        "status": "failed",
                        "source_commit": "b" * 40,
                        "error": common_error,
                    },
                ],
                "totals": {"total": 2, "success": 0, "failed": 2},
            },
        )

        response = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages?job_id={job_id}"),
            contest_slug,
            "alice",
            job_id=job_id,
        )

        self.assertEqual(response.status_code, 200)
        html = response.body.decode("utf-8")
        self.assertIn("Statements &amp; Builds", html)
        self.assertIn("contest-build-grid", html)
        self.assertIn("Build Contest PDF", html)
        self.assertIn('id="contest-pdf-language"', html)
        self.assertIn("Build ICPC Package Bundle", html)
        self.assertIn("Build History", html)
        self.assertIn("View Report", html)
        self.assertIn('id="job-report"', html)
        self.assertIn("table-row-selected", html)
        self.assertEqual(html.count(common_error), 1)
        self.assertNotIn("head_commit", html)

    def test_package_job_groups_an_identical_error_once(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("package-common-error")
        for index in ("A", "B"):
            problem_slug = f"bob/package-denied-{index.lower()}-{self.random_id('problem')}"
            workspace_service.ensure_problem(problem_slug)
            problem_row = db_fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
            self.assertIsNotNone(problem_row)
            config.contest_service.add_problem(
                contest_id,
                index,
                int(problem_row["id"]),
                actor_user_id,
            )

        response = contest_packages_build_start(contest=contest_slug, user="alice")
        query = parse_qs(urlparse(str(response.headers["location"])).query)
        job_id = query["job_id"][0]
        completed = _wait_for_row(
            "SELECT status FROM contest_jobs WHERE id=? AND finished_at IS NOT NULL",
            [job_id],
        )

        self.assertIsNotNone(completed)
        self.assertEqual(str(completed["status"]), "failed")
        summary = read_contest_job_summary(contest_id, job_id)
        self.assertEqual(summary["common_error"], "read access to problem is required")
        self.assertEqual(
            {str(row["error"]) for row in summary["results"]},
            {"read access to problem is required"},
        )

    def test_contest_packages_exposes_statement_sources_and_uploads_resources(self) -> None:
        contest_slug = f"contest-stmt-src-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Statement Source Contest")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)

        page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Statement Sources", html)
        self.assertIn("Edit statements.tex", html)
        self.assertIn("Edit olymp.sty", html)
        self.assertIn("default, not saved yet", html)

        save_resp = contest_statement_source_save(
            contest=contest_slug,
            user="alice",
            language="english",
            path="olymp.sty",
            content="% custom contest style\n",
        )
        self.assertEqual(save_resp.status_code, 303)
        self.assertIn("source_path=olymp.sty", str(save_resp.headers.get("location", "")))
        source_root = config.contest_service.contest_source_root(contest_slug)
        self.assertEqual(
            (source_root / "statements" / "english" / "olymp.sty").read_text(encoding="utf-8"),
            "% custom contest style\n",
        )
        row = db_fetch_one(
            "SELECT key FROM contest_attachments WHERE contest_id=? AND key=?",
            [contest_id, "statements/english/olymp.sty"],
        )
        self.assertIsNotNone(row)

        ftl_resp = contest_statement_source_save(
            contest=contest_slug,
            user="alice",
            language="english",
            path="statements.ftl",
            content="FTL template\r\n",
        )
        self.assertEqual(ftl_resp.status_code, 303)
        self.assertEqual(
            (source_root / "statements" / "english" / "statements.ftl").read_text(encoding="utf-8"),
            "FTL template\n",
        )

        upload_resp = asyncio.run(
            contest_statement_source_upload(
                contest=contest_slug,
                user="alice",
                language="english",
                path="logos/",
                upload=self._FakeUpload("logo.png", b"PNG"),
            )
        )
        self.assertEqual(upload_resp.status_code, 303)
        self.assertEqual((source_root / "statements" / "english" / "logos" / "logo.png").read_bytes(), b"PNG")
        row = db_fetch_one(
            "SELECT key FROM contest_attachments WHERE contest_id=? AND key=?",
            [contest_id, "statements/english/logos/logo.png"],
        )
        self.assertIsNotNone(row)

        updated_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages", "language=english&source_path=olymp.sty"),
            contest_slug,
            "alice",
            language="english",
            source_path="olymp.sty",
        )
        self.assertEqual(updated_page.status_code, 200)
        updated_html = updated_page.body.decode("utf-8", errors="replace")
        self.assertIn("% custom contest style", updated_html)
        self.assertIn("logos/logo.png", updated_html)
        file_resp = contest_statement_source_file(
            contest=contest_slug,
            user="alice",
            language="english",
            path="logos/logo.png",
        )
        self.assertEqual(file_resp.status_code, 200)

    def test_contest_packages_queues_selected_problem_language(self) -> None:
        problem_slug = f"alice/contest-language-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        workspace = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(workspace, "chinese")
        git_service.commit(workspace, "add chinese statement", "alice", "alice@polygonlike.local")
        git_service.push(workspace, "main")

        contest_slug = f"contest-language-{uuid.uuid4().hex[:8]}"
        self._create_contest(contest_slug, "Language Contest")
        contest_problems_add(
            contest=contest_slug,
            user="alice",
            problem_slugs=[problem_slug],
            q="",
        )

        page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages"),
            contest_slug,
            "alice",
        )
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('<option value="chinese"', html)
        self.assertIn("Insert blank pages after odd statements", html)

        with patch(
            "app.impl.contest.package._queue_contest_job",
            return_value=("pdf-language-job", True, "queued"),
        ) as queue_job:
            response = contest_packages_preview_start(
                contest=contest_slug,
                user="alice",
                language="chinese",
                insert_blank_pages=True,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(queue_job.call_args.kwargs["language"], "chinese")
        self.assertTrue(queue_job.call_args.kwargs["insert_blank_pages"])
        redirect_query = parse_qs(urlparse(str(response.headers["location"])).query)
        self.assertEqual(redirect_query["language"], ["chinese"])
        self.assertEqual(redirect_query["job_id"], ["pdf-language-job"])

    def test_contest_pdf_and_package_jobs_create_artifacts(self) -> None:
        problem_slug = f"alice/ui-contest-pack-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "README.problem.md").write_text("contest package test\n", encoding="utf-8")
        (ws / "statement" / "olymp.sty").write_text(
            "% problem style\n\\definecolor{gapfill}{RGB}{255,225,225}\n\\colorlet{gapline}{red!60!black}\n",
            encoding="utf-8",
        )
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Problem legend\n", encoding="utf-8")
        (ws / "statement-sections" / "english" / "notes.tex").write_text(
            "\\usetikzlibrary{arrows.meta,calc}\n\\begin{tikzpicture}\\end{tikzpicture}\n",
            encoding="utf-8",
        )
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
                    "package_bytes": b"\\\\documentclass{article}\n\\\\usepackage{olymp}\n%\\intentionallyblankpagestrue\n\\\\begin{document}\n\\\\import{../../problems/src-problem/statements/}{./problem.tex}\n\\\\end{document}\n",
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
            if command[0] == "xelatex":
                (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n%mock contest pdf\n")
                (cwd / "statements.log").write_text("xelatex ok\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            return ExecResult(backend="test", status="error", returncode=1, elapsed_ms=1, stdout="", stderr="unexpected command")

        package_calls: list[dict[str, object]] = []
        package_prepare_calls: list[dict[str, object]] = []

        def _fake_prepare_package_verification(
            problem: str,
            username: str,
            *,
            actor_user_id: int,
            problem_id: int,
            workspace_id: int,
            source_commit: str,
            requested_verification_id: str,
            ok_only: bool,
        ) -> str:
            package_prepare_calls.append(
                {
                    "problem": problem,
                    "username": username,
                    "actor_user_id": actor_user_id,
                    "problem_id": problem_id,
                    "workspace_id": workspace_id,
                    "source_commit": source_commit,
                    "requested_verification_id": requested_verification_id,
                    "ok_only": ok_only,
                }
            )
            return f"ver-{uuid.uuid4().hex[:12]}"

        def _fake_create_export(
            problem: str,
            verification_id: str,
            export_type: str,
            *,
            workspace_id: int,
            source_commit: str,
        ):
            self.assertEqual(str(export_type), "icpc")
            package_calls.append(
                {
                    "problem": problem,
                    "verification_id": verification_id,
                    "workspace_id": workspace_id,
                    "source_commit": source_commit,
                }
            )
            export_dir = (
                Path(config.settings.artifacts_root)
                / "exports"
                / problem.replace("/", "-")
                / "e-package-test"
            )
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
            patch(
                "app.impl.contest.shared.prepare_icpc_export_verification",
                side_effect=_fake_prepare_package_verification,
            ),
            patch.object(config.export_service, "create_export", side_effect=_fake_create_export),
        ):
            preview_start = contest_packages_preview_start(
                contest=contest_slug,
                user="alice",
                language="english",
                insert_blank_pages=True,
            )
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

            (ws / "README.problem.md").write_text("dirty content excluded from package\n", encoding="utf-8")
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
        self.assertEqual(len(package_calls), 1)
        self.assertEqual(len(package_prepare_calls), 1)
        self.assertTrue(bool(package_prepare_calls[0]["ok_only"]))
        self.assertEqual(str(package_prepare_calls[0]["source_commit"]), commit_id)
        self.assertGreater(int(package_calls[0]["workspace_id"]), 0)
        self.assertEqual(str(package_calls[0]["source_commit"]), commit_id)
        package_summary = read_contest_job_summary(contest_id, package_job_id)
        package_result = list(package_summary["results"])[0]
        self.assertEqual(package_result["source_commit"], commit_id)
        self.assertNotIn("head_commit", package_result)

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
        contest_statements_text = (compile_root / "statements" / "english" / "statements.tex").read_text(encoding="utf-8")
        self.assertIn("\\intentionallyblankpagestrue", contest_statements_text.splitlines())
        self.assertNotIn("%\\intentionallyblankpagestrue", contest_statements_text.splitlines())
        self.assertIn("\\usepackage{xeCJK}", contest_statements_text)
        self.assertIn("\\setCJKmainfont{Noto Serif CJK SC}", contest_statements_text)
        self.assertIn("ItalicFont={[FandolKai-Regular.otf]}", contest_statements_text)
        self.assertIn("\\definecolor{gapfill}{RGB}{255,225,225}", contest_statements_text)
        self.assertIn("\\colorlet{gapline}{red!60!black}", contest_statements_text)
        self.assertIn("\\usetikzlibrary{arrows.meta,calc}", contest_statements_text)
        self.assertTrue((compile_root / "statements" / "english" / "olymp.sty").is_file())
        self.assertEqual((compile_root / "statements" / "english" / "olymp.sty").read_text(encoding="utf-8"), "% contest style\n")
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").is_file())
        rendered_problem_tex = (compile_root / "problems" / "src-problem" / "statements" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn("\\Example", rendered_problem_tex)
        self.assertIn("sample.001.in", rendered_problem_tex)
        self.assertIn("sample.001.ans", rendered_problem_tex)
        self.assertNotIn("\\usetikzlibrary", rendered_problem_tex)
        self.assertIn("\\begin{tikzpicture}", rendered_problem_tex)
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.in").is_file())
        self.assertTrue((compile_root / "problems" / "src-problem" / "statements" / "english" / "sample.001.ans").is_file())
        self.assertEqual(len(sample_sync_calls), 1)
        self.assertEqual(sample_sync_calls[0][0], problem_slug)
        self.assertEqual(sample_sync_calls[0][1], "alice")
        command_names = [command[0] for command, _cwd, _mounts, _env in tex_commands]
        self.assertIn("extractbb", command_names)
        self.assertIn("mpost", command_names)
        self.assertIn("xelatex", command_names)
        self.assertNotIn("latex", command_names)
        self.assertNotIn("dvips", command_names)
        self.assertNotIn("dvipdfmx", command_names)
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
        xelatex_commands = [command for command, _cwd, _mounts, _env in tex_commands if command[0] == "xelatex"]
        self.assertEqual(len(xelatex_commands), 2)
        for command in xelatex_commands:
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
        self.assertNotIn("\\intentionallyblankpagestrue", wrapper_text)

    def test_contest_packages_surfaces_top_level_job_error(self) -> None:
        contest_slug = f"ui-contest-job-error-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Contest Job Error")
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        job_id = f"cj-{uuid.uuid4().hex[:12]}"
        db_execute(
            """
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [
                job_id,
                contest_id,
                int(alice_row["id"]),
                "pdf",
                "failed",
                "2026-03-01T10:00:00+00:00",
                "2026-03-01T10:00:02+00:00",
            ],
        )
        write_contest_job_summary(
            contest_id,
            job_id,
            {"job_type": "pdf", "error": "contest statement default language is missing"},
        )

        status_resp = contest_packages_job_status(contest=contest_slug, user="alice", job_id=job_id)
        self.assertEqual(status_resp.status_code, 200)
        status_payload = json.loads(status_resp.body.decode("utf-8"))
        self.assertEqual(status_payload.get("error"), "contest statement default language is missing")
        self.assertEqual(status_payload.get("summary", {}).get("error"), "contest statement default language is missing")

        packages_page = contest_packages_page(
            _request(f"/contests/{contest_slug}/packages?job_id={job_id}"),
            contest_slug,
            "alice",
            job_id=job_id,
        )
        self.assertEqual(packages_page.status_code, 200)
        packages_html = packages_page.body.decode("utf-8", errors="replace")
        self.assertIn("Selected Job Report", packages_html)
        self.assertIn("contest statement default language is missing", packages_html)
        self.assertIn("No per-problem report was produced", packages_html)

    def test_contest_pdf_job_uses_fallback_statement_template(self) -> None:
        problem_slug = f"alice/ui-contest-fallback-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem_slug)
        workspace_service.grant_repo_access(problem_slug, "alice", "owner")
        ws = Path(workspace_service.ensure_workspace(problem_slug, "alice"))
        ensure_statement_language_sources(ws, "english")
        (ws / "statement-sections" / "english" / "legend.tex").write_text("Fallback statement body\n", encoding="utf-8")
        (ws / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "manual" / "001.in").write_text("1\n", encoding="utf-8")
        (ws / "tests" / "spec.json").write_text(
            json.dumps({"version": 2, "tests": [{"id": "001", "kind": "manual", "sample": False}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        commit_id = git_service.commit(ws, "seed fallback statement", "alice", "alice@polygonlike.local")
        git_service.push(ws, "main")
        self.assertRegex(str(commit_id), r"^[0-9a-f]{40}$")

        contest_slug = f"ui-contest-fallback-{uuid.uuid4().hex[:8]}"
        contest_id = self._create_contest(contest_slug, "Fallback Contest")
        add_resp = contest_problems_add(contest=contest_slug, user="alice", problem_slugs=[problem_slug], q="")
        self.assertEqual(add_resp.status_code, 303)
        alice_row = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(alice_row)
        config.contest_service.write_statement_source_file(
            contest_id=contest_id,
            contest_slug=contest_slug,
            actor_user_id=int(alice_row["id"]),
            key="statements/english/logo.png",
            package_bytes=b"PNG",
        )

        def _fake_sandbox_run(spec):
            command = [str(token) for token in spec.command]
            cwd = Path(spec.cwd)
            if command[0] == "xelatex":
                (cwd / "statements.pdf").write_bytes(b"%PDF-1.4\n%mock contest pdf\n")
                (cwd / "statements.log").write_text("xelatex ok\n", encoding="utf-8")
                return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")
            return ExecResult(backend="test", status="ok", returncode=0, elapsed_ms=1, stdout="", stderr="")

        with (
            patch.object(config.tex_compile_service.sandbox, "run", side_effect=_fake_sandbox_run),
            patch.object(config.preview_service, "sync_sample_payloads_for_snapshot", side_effect=RuntimeError("judgehost is offline")),
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

        summary = read_contest_job_summary(contest_id, preview_job_id)
        self.assertEqual(str(summary.get("language") or ""), "english")
        self.assertEqual(summary.get("totals", {}).get("success"), 1)
        self.assertIn("sample sync skipped", str(summary.get("results", [{}])[0].get("warning", "")))
        contest_job_root = config.contest_service.job_root(contest_slug, preview_job_id)
        statements_tex = contest_job_root / "contest-pdf-src" / "statements" / "english" / "statements.tex"
        self.assertTrue(statements_tex.is_file())
        statements_text = statements_tex.read_text(encoding="utf-8")
        self.assertIn("\\usepackage{olymp}", statements_text)
        self.assertIn("\\usepackage{xeCJK}", statements_text)
        self.assertIn("\\setCJKmainfont{Noto Serif CJK SC}", statements_text)
        self.assertIn("ItalicFont={[FandolKai-Regular.otf]}", statements_text)
        self.assertIn("\\usepackage{tikz}", statements_text)
        self.assertIn("\\usepackage{pgfplots}", statements_text)
        self.assertIn("\\usepackage{algorithm}", statements_text)
        self.assertIn("\\usepackage{algpseudocode}", statements_text)
        self.assertIn("%\\intentionallyblankpagestrue", statements_text.splitlines())
        self.assertNotIn("\\intentionallyblankpagestrue", statements_text.splitlines())
        self.assertIn("/statements/english/", statements_text)
        self.assertTrue((statements_tex.parent / "olymp.sty").is_file())
        self.assertEqual((statements_tex.parent / "logo.png").read_bytes(), b"PNG")

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
