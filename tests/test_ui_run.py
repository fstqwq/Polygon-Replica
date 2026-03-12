from __future__ import annotations

from tests.ui_support import (
    HTTPException,
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _request,
    _wait_for_row,
    _wait_for_run_execute_workers,
    _wait_for_verification_workers,
    asyncio,
    build_page,
    build_service,
    config,
    db,
    general_page,
    io,
    json,
    os,
    parse_qs,
    patch,
    preview_page,
    quote_plus,
    run_details_page,
    run_details_test_fragment,
    run_execute,
    run_export_impl,
    run_new_page,
    run_page,
    run_service,
    statement_sources_signature,
    tests_spec_add_gen,
    tests_spec_edit,
    tests_spec_add_manual,
    tests_spec_add_manual_upload,
    tests_spec_delete,
    tests_spec_gen_script_save,
    tests_spec_payload_download,
    tests_spec_payload_upload,
    tests_spec_reindex,
    threading,
    time,
    urlparse,
    uuid,
    verification_start,
    workspace_impl,
    workspace_service,
)
import app.impl.workspace.context_job as workspace_context_job


class TestUIRun(UIBaseSuite):
    def test_tests_spec_crud_updates_spec_file_and_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        manual_dir = ws / "tests" / "manual"
        generator_dir = ws / "tests" / "generator"
        spec_path.unlink(missing_ok=True)
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        add_manual = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            manual_input="1 2 3  \r\n4 5\t \r\n",
        )
        self.assertEqual(add_manual.status_code, 303)
        add_manual_loc = str(add_manual.headers.get("location", ""))
        self.assertIn("/problems/alice/sample/alice/tests", add_manual_loc)
        add_manual_query = parse_qs(urlparse(add_manual_loc).query)
        self.assertIsNone(add_manual_query.get("mode"))
        self.assertEqual(add_manual_query.get("focus"), ["1"])

        add_gen = tests_spec_add_gen(
            problem="alice/sample",
            user="alice",
            test_id="002",
            command="gen 10 20",
        )
        self.assertEqual(add_gen.status_code, 303)
        add_gen_loc = str(add_gen.headers.get("location", ""))
        add_gen_query = parse_qs(urlparse(add_gen_loc).query)
        self.assertIsNone(add_gen_query.get("mode"))
        self.assertEqual(add_gen_query.get("focus"), ["2"])

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 2)
        self.assertEqual(tests[0].get("id"), "001")
        self.assertEqual(tests[0].get("kind"), "manual")
        self.assertEqual(tests[1].get("id"), "002")
        self.assertEqual(tests[1].get("kind"), "gen")
        manual_payload = (manual_dir / "001.in").read_text(encoding="utf-8")
        self.assertEqual(manual_payload, "1 2 3\n4 5\n")
        self.assertNotIn("\r", manual_payload)
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 10 20")

        edit_gen = tests_spec_edit(
            problem="alice/sample",
            user="alice",
            index="2",
            test_id="002",
            kind="gen",
            sample="1",
            payload="gen 99",
        )
        self.assertEqual(edit_gen.status_code, 303)
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 99")

        reindex = tests_spec_reindex(
            problem="alice/sample",
            user="alice",
            test_id="002",
            target_index="1",
        )
        self.assertEqual(reindex.status_code, 303)
        reindex_loc = str(reindex.headers.get("location", ""))
        self.assertIn("focus=1", reindex_loc)

        delete_second = tests_spec_delete(
            problem="alice/sample",
            user="alice",
            index="2",
        )
        self.assertEqual(delete_second.status_code, 303)

        payload_after = json.loads(spec_path.read_text(encoding="utf-8"))
        tests_after = payload_after.get("tests") or []
        self.assertEqual(len(tests_after), 1)
        self.assertEqual(tests_after[0].get("kind"), "gen")
        self.assertEqual(tests_after[0].get("id"), "002")
        self.assertTrue(bool(tests_after[0].get("sample")))
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 99")

        page = build_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests/spec.json", html)
        self.assertIn("tests/generator/002.in", html)
        self.assertIn("gen 99", html)

    def test_tests_spec_gen_script_save_adds_and_removes_gen_entries(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        spec_path = ws / "tests" / "spec.json"
        manual_dir = ws / "tests" / "manual"
        generator_dir = ws / "tests" / "generator"
        spec_path.unlink(missing_ok=True)
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        self.assertEqual(
            tests_spec_add_manual(problem="alice/sample", user="alice", test_id="001", manual_input="7\n").status_code,
            303,
        )
        self.assertEqual(
            tests_spec_add_gen(problem="alice/sample", user="alice", test_id="002", command="gen 10 1").status_code,
            303,
        )
        self.assertEqual(
            tests_spec_add_gen(problem="alice/sample", user="alice", test_id="003", command="gen 20 2").status_code,
            303,
        )

        updated = tests_spec_gen_script_save(
            problem="alice/sample",
            user="alice",
            gen_script_text="gen 10 1\ngen 30 3\n",
        )
        self.assertEqual(updated.status_code, 303)
        self.assertTrue(str(updated.headers.get("location", "")).endswith("/problems/alice/sample/alice/tests"))

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual([(row.get("id"), row.get("kind")) for row in tests], [("001", "manual"), ("002", "gen"), ("003", "gen")])
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 10 1")
        self.assertEqual((generator_dir / "003.in").read_text(encoding="utf-8"), "gen 30 3")

        cleared = tests_spec_gen_script_save(problem="alice/sample", user="alice", gen_script_text="")
        self.assertEqual(cleared.status_code, 303)
        payload_after = json.loads(spec_path.read_text(encoding="utf-8"))
        tests_after = payload_after.get("tests") or []
        self.assertEqual([(row.get("id"), row.get("kind")) for row in tests_after], [("001", "manual")])
        self.assertFalse((generator_dir / "002.in").exists())
        self.assertFalse((generator_dir / "003.in").exists())

    def test_tests_spec_large_manual_disables_inline_editor_and_shows_payload_actions(self) -> None:
        ws_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace = Path(str(ws_ctx["workspace"]["path"]))
        spec_path = workspace / "tests" / "spec.json"
        manual_dir = workspace / "tests" / "manual"
        generator_dir = workspace / "tests" / "generator"
        spec_path.unlink(missing_ok=True)
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        add_manual = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        huge_manual = ("A" * 200000) + "\n"
        (manual_dir / "001.in").write_text(huge_manual, encoding="utf-8")

        page = build_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Inline payload editor is disabled for large manual tests.", html)
        self.assertIn("Showing first 256 bytes.", html)
        self.assertIn("/tests/spec/payload/download?index=1", html)
        self.assertIn("/tests/spec/payload/upload", html)
        self.assertNotIn("/tests/spec/update", html)
        self.assertIn("A" * 256, html)
        self.assertNotIn("A" * 512, html)

    def test_tests_spec_manual_payload_upload_and_download_routes(self) -> None:
        class _FakeUpload:
            def __init__(self, data: bytes):
                self._buf = io.BytesIO(data)

            async def read(self, size: int = -1) -> bytes:
                return self._buf.read(size)

            async def close(self) -> None:
                return None

        ws_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace = Path(str(ws_ctx["workspace"]["path"]))
        spec_path = workspace / "tests" / "spec.json"
        manual_dir = workspace / "tests" / "manual"
        generator_dir = workspace / "tests" / "generator"
        spec_path.unlink(missing_ok=True)
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        add_manual = tests_spec_add_manual(
            problem="alice/sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        upload_payload = _FakeUpload(b"7 8 9  \r\n10 11\t \r\n")
        uploaded = asyncio.run(
            tests_spec_payload_upload(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=upload_payload,
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("/problems/alice/sample/alice/tests", uploaded.headers.get("location", ""))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "7 8 9\n10 11\n")

        downloaded = tests_spec_payload_download(problem="alice/sample", user="alice", index="1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("001.in", str(downloaded.headers.get("content-disposition", "")))

    def test_tests_spec_add_manual_upload_route(self) -> None:
        class _FakeUpload:
            def __init__(self, data: bytes):
                self._buf = io.BytesIO(data)

            async def read(self, size: int = -1) -> bytes:
                return self._buf.read(size)

            async def close(self) -> None:
                return None

        ws_ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace = Path(str(ws_ctx["workspace"]["path"]))
        spec_path = workspace / "tests" / "spec.json"
        manual_dir = workspace / "tests" / "manual"
        generator_dir = workspace / "tests" / "generator"
        spec_path.unlink(missing_ok=True)
        if manual_dir.exists():
            for p in manual_dir.glob("*.in"):
                p.unlink(missing_ok=True)
        if generator_dir.exists():
            for p in generator_dir.glob("*.in"):
                p.unlink(missing_ok=True)

        upload = _FakeUpload(b"11 22  \r\n33 44\t \r\n")
        created = asyncio.run(
            tests_spec_add_manual_upload(
                problem="alice/sample",
                user="alice",
                test_id="",
                sample="1",
                manual_upload=upload,
            )
        )
        self.assertEqual(created.status_code, 303)
        location = str(created.headers.get("location", ""))
        self.assertIn("/problems/alice/sample/alice/tests", location)
        query = parse_qs(urlparse(location).query)
        self.assertIsNone(query.get("mode"))
        self.assertEqual(query.get("focus"), ["1"])

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("id")), "001")
        self.assertEqual(str(tests[0].get("kind")), "manual")
        self.assertTrue(bool(tests[0].get("sample")))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "11 22\n33 44\n")

    def test_tests_page_includes_templates_examples_and_mode_controls(self) -> None:
        tests_spec_add_manual(problem="alice/sample", user="alice", test_id="001", manual_input="1\n")
        page = build_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('data-popup-open="tests-add-manual-popup"', html)
        self.assertIn('data-popup-open="tests-upload-manual-popup"', html)
        self.assertIn('data-popup-open="tests-reindex-popup-1"', html)
        self.assertIn('action="/problems/alice/sample/alice/tests/spec/gen-script"', html)
        self.assertRegex(
            html,
            r'<textarea[^>]*id="tests-gen-script-text"[^>]*data-code-editor="1"[^>]*data-code-path="tests/spec/gen-script\.txt"[^>]*data-code-height="220"[^>]*data-code-wrap="1"[^>]*>',
        )
        self.assertIn('action="/problems/alice/sample/alice/tests/spec/reindex"', html)
        self.assertIn('action="/problems/alice/sample/alice/tests/spec/add-manual-upload"', html)
        self.assertIn('class="tests-editor-table"', html)
        self.assertIn("<th>Test</th>", html)
        self.assertNotIn("Move Up", html)
        self.assertNotIn("Move Down", html)
        self.assertIn('placeholder="3&#10;1 2 3"', html)
        self.assertIn('placeholder="gen 10 1&#10;gen 20 2"', html)
        self.assertIn("Template: each submission input as plain text.", html)
        self.assertNotIn("Batch Manual", html)
        self.assertNotIn("Batch Generator", html)

    def test_run_execute_without_tests_triggers_implicit_tests_generation(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        workspace_id = int(ctx["workspace"]["id"])
        db.execute("DELETE FROM runs WHERE workspace_id=?", [workspace_id])
        db.execute("DELETE FROM builds WHERE workspace_id=?", [workspace_id])

        resp = run_execute(
            problem="alice/sample",
            user="alice",
            build_id="",
            solution_paths=["solutions/accepted.cpp"],
            submission_upload=None,
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/run/details?invocation_id=", loc)
        run_messages = _flash_messages_from_response(resp)
        self.assertTrue(run_messages)
        self.assertIn("verification running", run_messages[0])
        query = parse_qs(urlparse(loc).query)
        invocation_id = (query.get("invocation_id") or [""])[0]
        self.assertTrue(invocation_id)
        run_row = _wait_for_row(
            "SELECT id,status FROM runs WHERE workspace_id=? AND summary_json LIKE ? ORDER BY created_at DESC LIMIT 1",
            [workspace_id, f"%{invocation_id}%"],
            timeout_sec=10.0,
        )
        self.assertIsNotNone(run_row)
        self.assertIn(str(run_row["status"] or ""), {"running", "ok", "failed"})
        deadline = time.monotonic() + 12.0
        build_count = 0
        while time.monotonic() < deadline:
            build_rows = db.fetch_one("SELECT COUNT(*) AS c FROM builds WHERE workspace_id=?", [workspace_id])
            build_count = int(build_rows["c"] or 0) if build_rows is not None else 0
            if build_count >= 1:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(build_count, 1)

    def test_run_execute_uses_problem_mode_from_general_config(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        problem_cfg = ws / "config" / "problem.json"
        problem_cfg.parent.mkdir(parents=True, exist_ok=True)
        problem_cfg.write_text(json.dumps({"mode": "multi-pass"}, indent=2) + "\n", encoding="utf-8")

        resp = run_execute(
            problem="alice/sample",
            user="alice",
            build_id="",
            solution_paths=["solutions/accepted.cpp"],
            submission_upload=None,
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        query = parse_qs(urlparse(loc).query)
        invocation_id = (query.get("invocation_id") or [""])[0]
        self.assertTrue(invocation_id)
        row = _wait_for_row(
            "SELECT mode FROM runs WHERE workspace_id=? AND summary_json LIKE ? ORDER BY created_at DESC LIMIT 1",
            [int(workspace_service.workspace_context("alice/sample", "alice", include_recent=False)["workspace"]["id"]), f"%{invocation_id}%"],
            timeout_sec=8.0,
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["mode"]), "multi-pass")

    def test_run_execute_records_invocation_audit_before_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        observed = {"checked": False}

        def _fake_start_batch(*args, **kwargs) -> bool:
            invocation_id = str(kwargs.get("invocation_id") or "")
            invocation_run_ids = [str(item or "") for item in (kwargs.get("invocation_run_ids") or []) if str(item or "")]
            audit_row = db.fetch_one(
                """
                SELECT details_json
                FROM audit_log
                WHERE problem_id=? AND actor_user_id=? AND action='run.execute'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [problem_id, actor_user_id],
            )
            self.assertIsNotNone(audit_row)
            details = json.loads(str(audit_row["details_json"] or "{}"))
            self.assertEqual(str(details.get("status") or ""), "queued")
            self.assertEqual(str(details.get("invocation_id") or ""), invocation_id)
            self.assertEqual([str(item or "") for item in (details.get("run_ids") or [])], invocation_run_ids)
            self.assertEqual(int(details.get("run_count") or 0), len(invocation_run_ids))
            observed["checked"] = True
            return True

        with patch("app.impl.run_export.run.start_run_execute_batch", side_effect=_fake_start_batch):
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                build_id="",
                solution_paths=["solutions/accepted.cpp", "solutions/wa.cpp"],
                submission_upload=None,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertTrue(observed["checked"])
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/run/details?invocation_id=", loc)
        invocation_id = (parse_qs(urlparse(loc).query).get("invocation_id") or [""])[0]
        self.assertTrue(invocation_id)
        mapped_row = db.fetch_one(
            """
            SELECT details_json
            FROM audit_log
            WHERE problem_id=? AND actor_user_id=? AND action='run.execute'
              AND details_json LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, actor_user_id, f"%{invocation_id}%"],
        )
        self.assertIsNotNone(mapped_row)

    def test_run_execute_passes_selected_tests_to_runner(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        with patch("app.impl.run_export.run.start_run_execute_batch", return_value=True) as start_batch:
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                build_id="",
                solution_paths=["solutions/accepted.cpp"],
                test_names=["001.in", "003.in"],
                submission_upload=None,
            )
        self.assertEqual(resp.status_code, 303)
        select_messages = _flash_messages_from_response(resp)
        self.assertTrue(select_messages)
        self.assertIn("tests selected (2)", select_messages[0])
        start_batch.assert_called_once()
        kwargs = start_batch.call_args.kwargs
        self.assertEqual(kwargs.get("selected_test_names"), ["001.in", "003.in"])
        self.assertEqual(str(kwargs.get("run_mode") or ""), "pass-fail")
        targets = kwargs.get("targets")
        self.assertIsInstance(targets, list)
        self.assertTrue(targets)
        first = targets[0]
        self.assertEqual(str(first.get("submission_path") or ""), "solutions/accepted.cpp")
        self.assertEqual(str(first.get("expected_behavior") or ""), "accepted")

    def test_verification_start_requires_main_correct_solution_marker(self) -> None:
        problem = f"alice/verify-main-required-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        cfg_path = ws / "config" / "build.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.pop("accepted_solution_source", None)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        start_resp = verification_start(problem=problem, user="alice", page="statement")
        self.assertEqual(start_resp.status_code, 303)
        messages = _flash_messages_from_response(start_resp)
        self.assertTrue(messages)
        self.assertIn("verification failed: main correct solution is required", messages[0])

        row = db.fetch_one(
            """
            SELECT a.details_json
            FROM audit_log a
            JOIN problems p ON p.id=a.problem_id
            WHERE p.slug=? AND a.action='verification.start'
            ORDER BY a.created_at DESC
            LIMIT 1
            """,
            [problem],
        )
        self.assertIsNotNone(row)
        payload = json.loads(str(row["details_json"]))
        self.assertEqual(payload.get("status"), "failed")
        self.assertIn("main correct solution is required", str(payload.get("error") or ""))

    def test_verification_worker_reuses_buildsolve_for_accepted_solution(self) -> None:
        problem = f"alice/verify-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        build_id = self.random_id("b-verif-buildsolve-reuse")
        invocation_id = f"inv-verif-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-verif-accepted-reuse-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-verif-wa-reuse-{uuid.uuid4().hex[:8]}"
        buildsolve_run_id = f"r-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        buildsolve_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / buildsolve_run_id
        buildsolve_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                workspace_head,
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-03-11T00:00:00Z",
                "2026-03-11T00:00:01Z",
            ],
        )
        buildsolve_summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
            "error": "",
            "invocation": {
                "id": f"inv-{buildsolve_run_id}",
                "source": "build.solve",
                "run_ids": [buildsolve_run_id],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
                "reason": "",
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                buildsolve_run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(buildsolve_summary),
                str(buildsolve_root),
                "2026-03-11T00:00:02Z",
                "2026-03-11T00:00:03Z",
            ],
        )

        submitted_paths: list[str] = []

        def _fake_run_submission(**kwargs):
            run_id = str(kwargs.get("run_id") or "")
            source_path = str(kwargs.get("submission_path") or "")
            submitted_paths.append(source_path)
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            summary = {
                "mode": "pass-fail",
                "source": source_path,
                "tests": [{"test": "001.in", "verdict": "WA", "passes": [{"pass": 1, "verdict": "WA"}]}],
                "error": "",
                "invocation": {
                    "id": invocation_id,
                    "source": "verification.start",
                    "run_ids": [accepted_run_id, wa_run_id],
                    "expected_behavior": "wrong_answer",
                    "matched": True,
                    "completed": True,
                    "passed_all_tests": False,
                    "reason": "",
                },
            }
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    "pass-fail",
                    "ok",
                    json.dumps(summary),
                    str(run_root),
                    "2026-03-11T00:00:04Z",
                    "2026-03-11T00:00:05Z",
                ],
            )
            return run_id

        targets = [
            {"path": "solutions/accepted.cpp", "expected_behavior": "accepted", "run_id": accepted_run_id},
            {"path": "solutions/wa.cpp", "expected_behavior": "wrong_answer", "run_id": wa_run_id},
        ]
        with patch("app.impl.workspace.context_job._ensure_implicit_build", return_value=(build_id, False)):
            with patch.object(config.invocation_backend_service, "run_submission", side_effect=_fake_run_submission):
                workspace_context_job._run_verification_start_worker(
                    problem,
                    "alice",
                    actor_user_id=actor_user_id,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    workspace_head=workspace_head,
                    workspace_dirty=workspace_dirty,
                    targets=targets,
                    invocation_id=invocation_id,
                )

        self.assertEqual(submitted_paths, ["solutions/wa.cpp"])
        accepted_row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [accepted_run_id])
        self.assertIsNotNone(accepted_row)
        self.assertEqual(str(accepted_row["status"] or ""), "ok")
        accepted_summary = json.loads(str(accepted_row["summary_json"] or "{}"))
        self.assertEqual(str(accepted_summary.get("source") or ""), "solutions/accepted.cpp")
        invocation = accepted_summary.get("invocation") if isinstance(accepted_summary, dict) else {}
        self.assertIsInstance(invocation, dict)
        self.assertEqual(str(invocation.get("source") or ""), "verification.start")

    def test_verification_sidebar_marks_stale_when_gen_chk_sol_tests_change(self) -> None:
        problem = f"alice/verify-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        signature = workspace_impl._verification_sources_signature(ws)
        signature_details = workspace_impl._verification_sources_signature_details(ws)

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "pass",
                        "workspace_head": workspace_head,
                        "workspace_dirty": workspace_dirty,
                        "verification_signature": signature,
                        "verification_signature_details": signature_details,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        (ws / "tests" / "manual" / "001.in").write_text("8\n", encoding="utf-8")

        page = general_page(_request(f"/problems/{problem}/alice/statement"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<span class="status-title(?: [^"]+)?">Verification</span>\s*<strong\s+class="status-value warn"[^>]*>\s*stale</strong>',
        )
        self.assertRegex(
            html,
            r'<strong\s+class="status-value warn"[^>]*data-tooltip="[^"]*changed: tests[^"]*"[^>]*>\s*stale</strong>',
        )
        self.assertIn("changed: tests", html)

    def test_verification_sidebar_marks_stale_when_general_info_changes(self) -> None:
        problem = f"alice/verify-stale-general-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        signature = workspace_impl._verification_sources_signature(ws)
        signature_details = workspace_impl._verification_sources_signature_details(ws)

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "pass",
                        "workspace_head": workspace_head,
                        "workspace_dirty": workspace_dirty,
                        "verification_signature": signature,
                        "verification_signature_details": signature_details,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        problem_cfg = ws / "config" / "problem.json"
        payload: dict[str, object] = {}
        if problem_cfg.exists():
            payload = json.loads(problem_cfg.read_text(encoding="utf-8"))
        payload["time_limit_ms"] = int(payload.get("time_limit_ms") or 2000) + 100
        problem_cfg.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        page = general_page(_request(f"/problems/{problem}/alice/statement"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            r'<span class="status-title(?: [^"]+)?">Verification</span>\s*<strong\s+class="status-value warn"[^>]*>\s*stale</strong>',
        )
        self.assertRegex(
            html,
            r'<strong\s+class="status-value warn"[^>]*data-tooltip="[^"]*changed: general info[^"]*"[^>]*>\s*stale</strong>',
        )
        self.assertIn("changed: general info", html)

    def test_run_page_shows_multi_solution_selector_without_mode_select(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")

        page = run_new_page(_request("/problems/alice/sample/alice/run/new", "solution_paths=solutions/wa.cpp"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("id=\"solution-paths\"", html)
        self.assertIn("type=\"checkbox\" name=\"solution_paths\"", html)
        self.assertIn("id=\"test-names\"", html)
        self.assertTrue(("type=\"checkbox\" name=\"test_names\"" in html) or ("No tests list available yet." in html))
        self.assertIn("id=\"solution-select-all\"", html)
        self.assertIn("id=\"solution-select-clear\"", html)
        self.assertIn("id=\"test-select-all\"", html)
        self.assertIn("id=\"test-select-clear\"", html)
        self.assertNotIn("name=\"submission_path\"", html)
        self.assertNotIn("name=\"mode\"", html)
        self.assertIn("solutions/accepted.cpp", html)
        self.assertIn("solutions/wa.cpp", html)
        self.assertIn("value=\"solutions/wa.cpp\" checked", html)

    def test_run_list_rejudge_link_uses_invocation_id_and_run_new_resolves_paths(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        invocation_id = f"inv-rerun-link-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-rerun-link-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-rerun-link-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-rerun-link")
        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK"}],
            "invocation": {
                "id": invocation_id,
                "run_ids": [run_ok, run_wa],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
            },
        }
        summary_wa = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": [{"test": "001.in", "verdict": "WA"}],
            "invocation": {
                "id": invocation_id,
                "run_ids": [run_ok, run_wa],
                "expected_behavior": "wrong_answer",
                "matched": True,
                "completed": True,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_ok,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_ok),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_ok),
                "2026-03-03T00:00:01Z",
                "2026-03-03T00:00:02Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_wa,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_wa),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_wa),
                "2026-03-03T00:00:03Z",
                "2026-03-03T00:00:04Z",
            ],
        )
        list_page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(list_page.status_code, 200)
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"/run/new?rerun_invocation_id={invocation_id}&force_recompile=1", list_html)
        self.assertNotIn("/run/new?solution_paths=", list_html)

        new_page = run_new_page(
            _request("/problems/alice/sample/alice/run/new", f"rerun_invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(new_page.status_code, 200)
        new_html = new_page.body.decode("utf-8", errors="replace")
        self.assertIn('name="solution_paths" value="solutions/accepted.cpp" checked', new_html)
        self.assertIn('name="solution_paths" value="solutions/wa.cpp" checked', new_html)

    def test_run_page_defaults_all_tests_checked_when_available(self) -> None:
        problem = f"alice/run-default-tests-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")

        add_manual_1 = tests_spec_add_manual(problem=problem, user="alice", test_id="001", manual_input="1\n")
        add_manual_2 = tests_spec_add_manual(problem=problem, user="alice", test_id="002", manual_input="2\n")
        self.assertEqual(add_manual_1.status_code, 303)
        self.assertEqual(add_manual_2.status_code, 303)

        page = run_new_page(_request(f"/problems/{problem}/alice/run/new"), problem, "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('name="test_names" value="001.in" checked', html)
        self.assertIn('name="test_names" value="002.in" checked', html)

    def test_run_page_uses_default_sidebar_without_invocation_table(self) -> None:
        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No verification yet.", html)
        self.assertNotIn("page-grid-wide", html)

    def test_run_list_keeps_run_ids_when_summary_is_oversized(self) -> None:
        problem = f"alice/inv-oversized-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-oversized-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-oversized-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-oversized-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-inv-oversized")
        oversized_blob = "x" * 70000
        base_summary = {
            "mode": "pass-fail",
            "tests": [{"test": "001.in", "verdict": "OK", "blob": oversized_blob}],
        }

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.execute",
                json.dumps(
                    {
                        "status": "queued",
                        "invocation_id": invocation_id,
                        "run_id": run_ok,
                        "run_ids": [run_ok, run_wa],
                        "run_count": 2,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        run_specs = [
            (run_ok, "solutions/accepted.cpp", "ok", "2026-02-23T00:00:01Z", "2026-02-23T00:00:02Z"),
            (run_wa, "solutions/wa.cpp", "running", "2026-02-23T00:00:03Z", ""),
        ]
        for run_id, source, status, created_at, finished_at in run_specs:
            summary = dict(base_summary)
            summary["source"] = source
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    "pass-fail",
                    status,
                    json.dumps(summary),
                    str(run_root),
                    created_at,
                    finished_at,
                ],
            )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        rows_by_id = {str(item.get("id") or ""): item for item in rows}
        self.assertIn(run_ok, rows_by_id)
        self.assertIn(run_wa, rows_by_id)
        self.assertEqual(int(rows_by_id[run_ok].get("run_count") or 0), 1)
        self.assertEqual(int(rows_by_id[run_wa].get("run_count") or 0), 1)

    def test_run_list_does_not_backfill_missing_run_ids_from_audit(self) -> None:
        problem = f"alice/inv-verify-map-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verify-map-{uuid.uuid4().hex[:8]}"
        run_a = f"r-verify-map-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-verify-map-b-{uuid.uuid4().hex[:8]}"

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "invocation_id": invocation_id,
                        "run_id": run_a,
                        "run_ids": [run_a, run_b],
                        "run_count": 2,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        summary = {"mode": "pass-fail", "source": "solutions/accepted.cpp", "tests": []}
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_a
        run_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_a,
                problem_id,
                workspace_id,
                self.random_id("b-verify-map"),
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:01Z",
                "",
            ],
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        rows_by_id = {str(item.get("id") or ""): item for item in rows}
        self.assertIn(run_a, rows_by_id)
        self.assertNotIn(run_b, rows_by_id)
        self.assertEqual(int(rows_by_id[run_a].get("run_count") or 0), 1)

    def test_run_list_shows_running_verification_from_audit_before_runs_exist(self) -> None:
        problem = f"alice/inv-audit-pending-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-audit-pending-{uuid.uuid4().hex[:8]}"
        run_a = f"r-audit-pending-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-audit-pending-b-{uuid.uuid4().hex[:8]}"

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "mode": "pass-fail",
                        "invocation_id": invocation_id,
                        "run_id": run_a,
                        "run_ids": [run_a, run_b],
                        "run_count": 2,
                        "submission_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                    }
                ),
                "2026-03-02T00:00:00Z",
            ],
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), invocation_id)
        self.assertEqual(str(rows[0].get("status") or ""), "running")
        self.assertTrue(bool(rows[0].get("has_running")))
        self.assertEqual(int(rows[0].get("run_count") or 0), 2)
        self.assertIn("solutions/accepted.cpp", str(rows[0].get("source_display") or ""))
        self.assertIn("solutions/wa.cpp", str(rows[0].get("source_display") or ""))
        self.assertEqual(str(rows[0].get("tests_label") or ""), "tests: in progress")

    def test_run_list_hides_build_solve_invocations(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        invocation_id = f"inv-buildsolve-{uuid.uuid4().hex[:8]}"
        run_id = f"r-buildsolve-{uuid.uuid4().hex[:8]}"
        invocation_generate_id = f"inv-buildgen-{uuid.uuid4().hex[:8]}"
        run_generate_id = f"r-buildgen-{uuid.uuid4().hex[:8]}"
        summary = {
            "source": "solutions/accepted.cpp",
            "invocation": {
                "id": invocation_id,
                "source": "build.solve",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
                "reason": "",
            },
            "tests": [
                {
                    "test": "001.in",
                    "verdict": "OK",
                    "passes": [{"pass": 1, "verdict": "OK"}],
                }
            ],
            "error": "",
        }
        summary_generate = {
            "source": "generators/gen.cpp",
            "invocation": {
                "id": invocation_generate_id,
                "source": "build.generate-input",
                "run_ids": [run_generate_id],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
                "reason": "",
            },
            "tests": [
                {
                    "test": "001.in",
                    "verdict": "OK",
                    "passes": [{"pass": 1, "verdict": "OK"}],
                }
            ],
            "error": "",
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                self.random_id("b-buildsolve"),
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id),
                "2026-03-03T00:00:00Z",
                "2026-03-03T00:00:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_generate_id,
                problem_id,
                workspace_id,
                self.random_id("b-buildgen"),
                "pass-fail",
                "ok",
                json.dumps(summary_generate),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_generate_id),
                "2026-03-03T00:00:02Z",
                "2026-03-03T00:00:03Z",
            ],
        )
        resp = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(resp.status_code, 200)
        html = resp.body.decode("utf-8", errors="replace")
        self.assertNotIn(f"invocation_id={invocation_id}", html)
        self.assertNotIn(f"invocation_id={invocation_generate_id}", html)
        self.assertNotIn("Main correct solution run", html)

    def test_run_details_uses_build_stage_rows_without_gen_main_badges(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])

        build_id = self.random_id("b-verif-stage-markers")
        invocation_id = f"inv-verif-stage-{uuid.uuid4().hex[:8]}"
        verify_run_id = f"r-verif-stage-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-buildgen-stage-{uuid.uuid4().hex[:8]}"
        main_run_id = f"r-buildsolve-stage-{uuid.uuid4().hex[:8]}"

        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        for run_id in (verify_run_id, gen_run_id, main_run_id):
            (Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id).mkdir(parents=True, exist_ok=True)

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-03-10T00:00:00Z",
                "2026-03-10T00:00:01Z",
            ],
        )

        def _summary(run_source: str, invocation_source: str, run_id: str) -> dict[str, object]:
            return {
                "mode": "pass-fail",
                "source": run_source,
                "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
                "error": "",
                "invocation": {
                    "id": f"inv-{run_id}",
                    "source": invocation_source,
                    "run_ids": [run_id],
                    "expected_behavior": "accepted",
                    "matched": True,
                    "completed": True,
                    "passed_all_tests": True,
                    "reason": "",
                },
            }

        run_rows = [
            (gen_run_id, build_id, _summary("generators/gen.cpp", "build.generate-input", gen_run_id), "ok", "2026-03-10T00:00:02Z"),
            (main_run_id, build_id, _summary("solutions/accepted.cpp", "build.solve", main_run_id), "ok", "2026-03-10T00:00:03Z"),
            (verify_run_id, build_id, _summary("solutions/accepted.cpp", "verification.start", verify_run_id), "ok", "2026-03-10T00:00:04Z"),
        ]
        for run_id, build_token, summary, status, created_at in run_rows:
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    build_token,
                    "pass-fail",
                    status,
                    json.dumps(summary),
                    str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id),
                    created_at,
                    "2026-03-10T00:00:05Z",
                ],
            )

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "pass",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": verify_run_id,
                        "run_ids": [verify_run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "ok",
                        "error": "",
                    }
                ),
                "2026-03-10T00:00:06Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("GEN AC", html)
        self.assertNotIn("MAIN AC", html)
        self.assertIn("001.in", html)
        self.assertIn("accepted.cpp", html)

    def test_run_list_orders_by_invocation_run_time_not_latest_member_time(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        old_invocation = f"inv-old-{uuid.uuid4().hex[:8]}"
        new_invocation = f"inv-new-{uuid.uuid4().hex[:8]}"
        old_run_1 = f"r-old-1-{uuid.uuid4().hex[:8]}"
        old_run_2 = f"r-old-2-{uuid.uuid4().hex[:8]}"
        new_run = f"r-new-{uuid.uuid4().hex[:8]}"

        def _summary(invocation_id: str, run_ids: list[str], source: str) -> dict[str, object]:
            return {
                "source": source,
                "invocation": {
                    "id": invocation_id,
                    "source": "verification.start",
                    "run_ids": run_ids,
                    "expected_behavior": "accepted",
                    "matched": True,
                    "completed": True,
                    "passed_all_tests": True,
                    "reason": "",
                },
                "tests": [{"test": "001.in", "verdict": "OK"}],
                "error": "",
            }

        run_specs = [
            (old_run_1, old_invocation, [old_run_1, old_run_2], "solutions/old-1.cpp", "2026-03-03T00:00:00Z"),
            (new_run, new_invocation, [new_run], "solutions/new.cpp", "2026-03-03T00:05:00Z"),
            # Later member of the old invocation must not move old invocation ahead of new invocation.
            (old_run_2, old_invocation, [old_run_1, old_run_2], "solutions/old-2.cpp", "2026-03-03T00:10:00Z"),
        ]
        for run_id, invocation_id, run_ids, source, created_at in run_specs:
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    problem_id,
                    workspace_id,
                    self.random_id("b-order"),
                    "pass-fail",
                    "ok",
                    json.dumps(_summary(invocation_id, run_ids, source)),
                    str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id),
                    created_at,
                    created_at,
                ],
            )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=10, actor_user_id=int(ctx["user"]["id"]))
        ordered_ids = [str(item.get("id") or "") for item in rows]
        self.assertIn(old_invocation, ordered_ids)
        self.assertIn(new_invocation, ordered_ids)
        self.assertLess(ordered_ids.index(new_invocation), ordered_ids.index(old_invocation))
        old_row = next((item for item in rows if str(item.get("id") or "") == old_invocation), {})
        self.assertEqual(str(old_row.get("created_at") or ""), "2026-03-03T00:00:00Z")

    def test_run_details_tracks_invocation_scope_across_async_refreshes(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-refresh-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-refresh-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-refresh-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-inv-refresh")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_ok_root = build_root / "logs" / f"run-{run_ok}"
        run_wa_root = build_root / "logs" / f"run-{run_wa}"
        run_ok_root.mkdir(parents=True, exist_ok=True)
        run_wa_root.mkdir(parents=True, exist_ok=True)

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.execute",
                json.dumps(
                    {
                        "invocation_id": invocation_id,
                        "run_id": run_ok,
                        "run_ids": [run_ok, run_wa],
                        "run_count": 2,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        first = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(first.status_code, 200)
        first_html = first.body.decode("utf-8", errors="replace")
        self.assertIn(run_ok, first_html)
        self.assertIn(run_wa, first_html)
        self.assertNotIn("Program 1", first_html)
        self.assertNotIn("Program 2", first_html)
        self.assertIn("Verification Progress", first_html)
        self.assertNotIn("Auto-refreshing every 2 seconds.", first_html)
        self.assertNotIn("window.location.reload", first_html)
        self.assertIn("No per-test details yet.", first_html)

        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
            "invocation": {
                "id": invocation_id,
                "source": "run.execute",
                "run_ids": [run_ok, run_wa],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
                "reason": "accepted solution passed all tests",
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_ok,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_ok),
                str(run_ok_root),
                "2026-02-23T00:00:01Z",
                "2026-02-23T00:00:02Z",
            ],
        )

        second = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(second.status_code, 200)
        second_html = second.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", second_html)
        self.assertIn(run_wa, second_html)
        self.assertNotIn("Program 2", second_html)
        self.assertNotIn("Expected match:", second_html)

        summary_wa = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": [{"test": "001.in", "verdict": "WA", "passes": [{"pass": 1, "verdict": "WA"}]}],
            "invocation": {
                "id": invocation_id,
                "source": "run.execute",
                "run_ids": [run_ok, run_wa],
                "expected_behavior": "wrong_answer",
                "matched": True,
                "completed": True,
                "passed_all_tests": False,
                "reason": "non-accepted solution failed as expected",
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_wa,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_wa),
                str(run_wa_root),
                "2026-02-23T00:00:03Z",
                "2026-02-23T00:00:04Z",
            ],
        )

        third = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(third.status_code, 200)
        third_html = third.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", third_html)
        self.assertIn("wa.cpp", third_html)
        self.assertNotIn("Expected match:", third_html)

    def test_rejudge_unavailable_consistent_between_list_and_details_while_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-rejudge-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-rejudge-ok-{uuid.uuid4().hex[:8]}"
        run_pending = f"r-rejudge-pending-{uuid.uuid4().hex[:8]}"
        run_ok_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_ok
        run_ok_root.mkdir(parents=True, exist_ok=True)
        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK"}],
            "invocation": {"id": invocation_id, "run_ids": [run_ok, run_pending], "matched": True, "completed": True, "passed_all_tests": True},
        }
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.execute",
                json.dumps({"invocation_id": invocation_id, "run_id": run_ok, "run_ids": [run_ok, run_pending], "run_count": 2}),
                "2026-02-23T00:00:00Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_ok,
                problem_id,
                workspace_id,
                self.random_id("b-rejudge"),
                "pass-fail",
                "ok",
                json.dumps(summary_ok),
                str(run_ok_root),
                "2026-02-23T00:00:01Z",
                "2026-02-23T00:00:02Z",
            ],
        )

        list_page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Rejudge unavailable:", list_html)
        self.assertNotIn(">Rejudge</a>", list_html)
        self.assertNotIn("/run/new?solution_paths=solutions%2Faccepted.cpp", list_html)

        details_page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        details_html = details_page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Rejudge unavailable:", details_html)
        self.assertNotIn(">Rejudge</button>", details_html)
        self.assertIn("Verification Progress", details_html)

    def test_run_cancel_marks_running_invocation_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-cancel-{uuid.uuid4().hex[:8]}"
        run_running = f"r-cancel-running-{uuid.uuid4().hex[:8]}"
        run_missing = f"r-cancel-missing-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_running
        run_root.mkdir(parents=True, exist_ok=True)
        summary_running = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests_total": 5,
            "tests": [],
            "invocation": {
                "id": invocation_id,
                "run_ids": [run_running, run_missing],
                "expected_behavior": "accepted",
                "completed": False,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_running,
                problem_id,
                workspace_id,
                self.random_id("b-cancel-run"),
                "pass-fail",
                "running",
                json.dumps(summary_running),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.execute",
                json.dumps(
                    {
                        "invocation_id": invocation_id,
                        "run_id": run_running,
                        "run_ids": [run_running, run_missing],
                        "run_count": 2,
                        "submission_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                    }
                ),
                "2026-02-23T00:00:01Z",
            ],
        )

        details_before = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        before_html = details_before.body.decode("utf-8", errors="replace")
        self.assertIn(">Cancel</a>", before_html)
        self.assertIn("Verification Progress", before_html)

        cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", invocation_id=invocation_id)
        self.assertEqual(cancel_resp.status_code, 303)
        self.assertIn(
            f"/problems/alice/sample/alice/run/details?invocation_id={invocation_id}",
            str(cancel_resp.headers.get("location", "")),
        )
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("cancel requested", cancel_messages[0])

        run_row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_running])
        self.assertIsNotNone(run_row)
        self.assertEqual(str(run_row["status"] or "").lower(), "failed")
        running_summary_after = json.loads(str(run_row["summary_json"] or "{}"))
        self.assertTrue(bool(running_summary_after.get("cancelled")))
        self.assertIn("cancelled by user", str(running_summary_after.get("error") or ""))
        self.assertEqual(int(running_summary_after.get("tests_total") or 0), 5)
        self.assertIsInstance(running_summary_after.get("tests"), list)
        self.assertEqual(len(running_summary_after.get("tests") or []), 0)

        missing_row = db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [run_missing])
        self.assertIsNotNone(missing_row)
        self.assertEqual(str(missing_row["status"] or "").lower(), "failed")
        missing_summary = json.loads(str(missing_row["summary_json"] or "{}"))
        self.assertEqual(int(missing_summary.get("tests_total") or 0), 0)
        self.assertEqual(list(missing_summary.get("tests") or []), [])
        self.assertTrue(bool(missing_summary.get("execution_skipped")))

        details_after = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        after_html = details_after.body.decode("utf-8", errors="replace")
        self.assertIn("Verification status:", after_html)
        self.assertIn("FAILED", after_html)
        self.assertNotIn(">Cancel</a>", after_html)

    def test_finalize_cancelled_builds_marks_build_failed_without_active_runs(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-cancel-finalize")
        run_id = f"r-cancel-finalize-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                json.dumps({"step": "run"}),
                str(build_root),
                "2026-03-05T00:00:00Z",
                "",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                "{}",
                str(run_root),
                "2026-03-05T00:00:01Z",
                "2026-03-05T00:00:02Z",
            ],
        )

        cancelled = run_export_impl._finalize_cancelled_builds([run_id], "verification cancelled by user")
        self.assertEqual(cancelled, 1)
        build_row = db.fetch_one("SELECT status,summary_json FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(build_row)
        self.assertEqual(str(build_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(build_row["summary_json"] or "{}"))
        self.assertTrue(bool(summary.get("cancelled")))
        self.assertIn("cancelled by user", str(summary.get("cancel_reason") or ""))

    def test_finalize_cancelled_builds_skips_when_other_active_runs_exist(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-cancel-active")
        run_cancelled_id = f"r-cancel-active-failed-{uuid.uuid4().hex[:8]}"
        run_active_id = f"r-cancel-active-running-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_cancelled_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_cancelled_id
        run_active_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_active_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_cancelled_root.mkdir(parents=True, exist_ok=True)
        run_active_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-05T00:00:00Z",
                "",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_cancelled_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                "{}",
                str(run_cancelled_root),
                "2026-03-05T00:00:01Z",
                "2026-03-05T00:00:02Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_active_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "running",
                "{}",
                str(run_active_root),
                "2026-03-05T00:00:03Z",
                "",
            ],
        )

        cancelled = run_export_impl._finalize_cancelled_builds([run_cancelled_id], "verification cancelled by user")
        self.assertEqual(cancelled, 0)
        build_row = db.fetch_one("SELECT status FROM builds WHERE id=?", [build_id])
        self.assertIsNotNone(build_row)
        self.assertEqual(str(build_row["status"] or "").strip().lower(), "running")

    def test_run_list_running_invocation_shows_in_progress_labels(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        invocation_id = f"inv-progress-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-progress-ok-{uuid.uuid4().hex[:8]}"
        run_running = f"r-progress-running-{uuid.uuid4().hex[:8]}"
        run_ids = [run_ok, run_running]

        def _tests(total: int) -> list[dict[str, object]]:
            return [{"test": f"{idx:03}.in", "verdict": "OK"} for idx in range(1, total + 1)]

        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": _tests(44),
            "invocation": {
                "id": invocation_id,
                "run_ids": run_ids,
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
            },
        }
        summary_running = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": _tests(18),
            "invocation": {
                "id": invocation_id,
                "run_ids": run_ids,
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }

        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_ok,
                problem_id,
                workspace_id,
                self.random_id("b-progress-ok"),
                "pass-fail",
                "ok",
                json.dumps(summary_ok),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_ok),
                "2026-02-23T00:10:00Z",
                "2026-02-23T00:10:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_running,
                problem_id,
                workspace_id,
                self.random_id("b-progress-running"),
                "pass-fail",
                "running",
                json.dumps(summary_running),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_running),
                "2026-02-23T00:10:02Z",
                "",
            ],
        )

        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests: up to 44 (in progress)", html)
        self.assertIn("1/1 completed matched (2 total)", html)
        self.assertNotIn("tests: 18-44 (varied)", html)
        self.assertNotIn("1/2 expected", html)
        self.assertIn("/problems/alice/sample/alice/run/cancel", html)
        self.assertIn(f'name="invocation_id" value="{invocation_id}"', html)
        self.assertIn(">Cancel</a>", html)

    def test_run_details_show_progress_placeholders_while_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-running-progress-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 5,
            "usage": {"tests": 5},
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-running-progress"),
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "",
            ],
        )

        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("0/5 tests finished", html)
        self.assertIn("test 1", html)
        self.assertIn("pending", html)

    def test_run_details_show_domjudge_case_rows_while_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-running-domjudge-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 3,
            "usage": {"tests": 3},
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-running-domjudge"),
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "",
            ],
        )

        fake_case_rows = [
            {
                "run_id": run_id,
                "test_name": "001.in",
                "status": "pending",
                "runresult": "",
                "cpu_sec": 0.0,
                "runtime_sec": 0.0,
                "wall_sec": 0.0,
                "memory_kb": 0,
            },
            {
                "run_id": run_id,
                "test_name": "002.in",
                "status": "leased",
                "runresult": "",
                "cpu_sec": 0.0,
                "runtime_sec": 0.0,
                "wall_sec": 0.0,
                "memory_kb": 0,
            },
            {
                "run_id": run_id,
                "test_name": "003.in",
                "status": "reported",
                "runresult": "wrong-answer",
                "cpu_sec": 0.004,
                "runtime_sec": 0.004,
                "wall_sec": 0.05,
                "memory_kb": 1024,
            },
        ]
        with patch.object(config.judgehost_task_service, "domjudge_case_cells_for_runs", return_value=fake_case_rows):
            page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
            detail = run_details_test_fragment(
                _request("/problems/alice/sample/alice/run/details/test-fragment", f"run_id={run_id}&test=003.in"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("002.in", html)
        self.assertIn("003.in", html)
        self.assertIn('invocation-cell-meta">pending<', html)
        self.assertIn('invocation-cell-meta">running<', html)
        self.assertIn('invocation-cell-meta">4ms/1MB<', html)
        self.assertIn(">WA<", html)
        self.assertIn('invocation-test-toggle" href="#run-test-detail-popup" data-popup-open="run-test-detail-popup" data-test-name="003.in"', html)
        self.assertNotIn("Showing first 3 placeholders", html)
        self.assertNotIn("No per-test details yet.", html)
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotIn("No detail.", detail_html)
        self.assertIn(">4ms cpu, 50ms wall<", detail_html)

    def test_run_details_uses_solution_progress_for_single_total(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-running-singular-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 1,
            "usage": {"tests": 1},
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-running-singular"),
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "",
            ],
        )

        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("0/1 tests finished", html)
        self.assertNotIn("tests reported", html)

    def test_run_details_shows_verification_lifecycle_for_verification_invocation(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-lifecycle-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-lifecycle-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-buildgen-lifecycle-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-lifecycle")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / "logs").mkdir(parents=True, exist_ok=True)
        (build_root / "logs" / "tests_meta.json").write_text(
            json.dumps(
                [
                    {"index": 1, "kind": "manual", "id": "m1", "sample": True},
                    {"index": 2, "kind": "gen", "id": "g2", "sample": False},
                    {"index": 3, "kind": "gen", "id": "g3", "sample": False},
                ]
            ),
            encoding="utf-8",
        )
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        gen_run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / gen_run_id
        run_root.mkdir(parents=True, exist_ok=True)
        gen_run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 3,
            "usage": {"tests": 3},
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        gen_summary = {
            "mode": "pass-fail",
            "source": "generators/generator.cpp",
            "tests": [
                {"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
                {"test": "002.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
                {"test": "003.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
            ],
            "invocation": {
                "id": f"inv-{gen_run_id}",
                "source": "build.generate-input",
                "run_ids": [gen_run_id],
                "expected_behavior": "accepted",
                "matched": True,
                "completed": True,
                "passed_all_tests": True,
                "reason": "",
            },
        }
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                gen_run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(gen_summary),
                str(gen_run_root),
                "2026-02-23T00:00:01Z",
                "2026-02-23T00:00:02Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:02Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "ok",
                    }
                ),
                "2026-02-23T00:00:03Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("Generate Inputs", html)
        self.assertIn("Check Expectations", html)
        self.assertIn("Generated tests", html)
        self.assertIn("3 tests", html)
        self.assertIn("Validated inputs", html)
        self.assertIn("3/3", html)
        self.assertIn("Solutions finished", html)
        self.assertIn("0/1", html)
        self.assertIn("Matched expectations", html)

    def test_run_details_does_not_fake_validated_inputs_without_generate_results(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-no-validate-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-no-validate-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-no-validate")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / "logs").mkdir(parents=True, exist_ok=True)
        (build_root / "logs" / "tests_meta.json").write_text(
            json.dumps(
                [
                    {"index": 1, "kind": "manual", "id": "m1", "sample": True},
                    {"index": 2, "kind": "gen", "id": "g2", "sample": False},
                    {"index": 3, "kind": "gen", "id": "g3", "sample": False},
                ]
            ),
            encoding="utf-8",
        )
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 3,
            "usage": {"tests": 3},
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:02Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "ok",
                    }
                ),
                "2026-02-23T00:00:03Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generated tests", html)
        self.assertIn("3 tests", html)
        self.assertNotIn("Validated inputs", html)

    def test_run_details_shows_generated_count_while_build_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-gen-running-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-gen-running-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-gen-running")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        tests_root.mkdir(parents=True, exist_ok=True)
        (tests_root / "001.in").write_text("1\n", encoding="utf-8")
        (tests_root / "002.in").write_text("2\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-03T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                    }
                ),
                "2026-03-03T00:00:01Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("Generate Inputs", html)
        self.assertIn("Generate Outputs", html)
        self.assertIn("Generated tests", html)
        self.assertIn("2 tests", html)
        self.assertIn("generating outputs", html)
        self.assertNotIn("Generated outputs", html)
        self.assertNotIn("0/2", html)

    def test_run_details_marks_step2_done_once_outputs_ready_even_if_build_running(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-step2-done-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-step2-done-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step2-done")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        ans_root = build_root / "ans"
        tests_root.mkdir(parents=True, exist_ok=True)
        ans_root.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 4):
            name = f"{idx:03}.in"
            (tests_root / name).write_text(f"{idx}\n", encoding="utf-8")
            (ans_root / f"{idx:03}.ans").write_text(f"{idx}\n", encoding="utf-8")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [
                {
                    "test": "001.in",
                    "passes": [{"pass": 1, "verdict": "OK", "time_ms": 1, "memory_kb": 1}],
                    "verdict": "OK",
                    "time_ms": 1,
                    "memory_kb": 1,
                    "feedback_files": [],
                }
            ],
            "tests_total": 3,
            "usage": {"tests": 3},
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-06T00:00:00Z",
                "",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-03-06T00:00:01Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "running",
                    }
                ),
                "2026-03-06T00:00:02Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("generated outputs 3/3", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">Completed<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )

    def test_run_details_uses_case_progress_for_step2_while_build_running(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-step2-progress-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step2-progress")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        ans_root = build_root / "ans"
        tests_root.mkdir(parents=True, exist_ok=True)
        ans_root.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 28):
            (tests_root / f"{idx:03}.in").write_text(f"{idx}\n", encoding="utf-8")
            (ans_root / f"{idx:03}.ans").write_text(f"{idx}\n", encoding="utf-8")
        planned_run_ids = [f"r-verif-step2-progress-{i:02d}-{uuid.uuid4().hex[:6]}" for i in range(1, 12)]
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-06T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": planned_run_ids[0],
                        "run_ids": planned_run_ids,
                        "run_count": len(planned_run_ids),
                        "build_id": build_id,
                        "build_status": "running",
                        "invocation_backend": "domjudge-judgehost",
                    }
                ),
                "2026-03-06T00:00:02Z",
            ],
        )
        with patch(
            "app.impl.workspace.run_view_lifecycle_builder._verification_buildsolve_case_progress",
            return_value={"total": 27, "reported": 18},
        ):
            page = run_details_page(
                _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generate Outputs", html)
        self.assertIn("18/27", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Pending<', re.IGNORECASE),
        )
        self.assertNotIn("Main correct solution run", html)

    def test_run_details_build_running_without_case_progress_keeps_step2_in_progress(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-step2-no-case-progress-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step2-no-case-progress")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        ans_root = build_root / "ans"
        tests_root.mkdir(parents=True, exist_ok=True)
        ans_root.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 28):
            (tests_root / f"{idx:03}.in").write_text(f"{idx}\n", encoding="utf-8")
            (ans_root / f"{idx:03}.ans").write_text(f"{idx}\n", encoding="utf-8")
        planned_run_ids = [f"r-verif-step2-no-case-progress-{i:02d}-{uuid.uuid4().hex[:6]}" for i in range(1, 12)]
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-06T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": planned_run_ids[0],
                        "run_ids": planned_run_ids,
                        "run_count": len(planned_run_ids),
                        "build_id": build_id,
                        "build_status": "running",
                        "invocation_backend": "domjudge-judgehost",
                    }
                ),
                "2026-03-06T00:00:02Z",
            ],
        )
        with patch(
            "app.impl.workspace.run_view_lifecycle_builder._verification_buildsolve_case_progress",
            return_value={"total": 0, "reported": 0},
        ):
            page = run_details_page(
                _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Pending<', re.IGNORECASE),
        )
        self.assertIn("generating outputs", html)
        self.assertNotIn("Generated outputs", html)
        self.assertNotIn("27/27", html)

    def test_run_details_buildsolve_invocation_stays_on_step2_while_running(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        invocation_id = f"inv-buildsolve-{uuid.uuid4().hex[:12]}"
        run_id = f"r-buildsolve-{uuid.uuid4().hex[:12]}"
        build_id = self.random_id("b-buildsolve-step2")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        ans_root = build_root / "ans"
        tests_root.mkdir(parents=True, exist_ok=True)
        ans_root.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 28):
            (tests_root / f"{idx:03}.in").write_text(f"{idx}\n", encoding="utf-8")
            (ans_root / f"{idx:03}.ans").write_text(f"{idx}\n", encoding="utf-8")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        tests_payload = [
            {
                "test": f"{idx:03}.in",
                "passes": [{"pass": 1, "verdict": "OK", "time_ms": 1, "memory_kb": 1}],
                "verdict": "OK",
                "time_ms": 1,
                "memory_kb": 1,
                "feedback_files": [],
            }
            for idx in range(1, 19)
        ]
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": tests_payload,
            "tests_total": 27,
            "usage": {"tests": 27},
            "invocation": {
                "id": invocation_id,
                "source": "build.solve",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-06T00:00:00Z",
                "",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-03-06T00:00:01Z",
                "",
            ],
        )
        with patch(
            "app.impl.workspace.run_view_lifecycle_builder._verification_buildsolve_case_progress",
            return_value={"total": 27, "reported": 18},
        ):
            page = run_details_page(
                _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generate Outputs", html)
        self.assertIn("18/27", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Pending<', re.IGNORECASE),
        )

    def test_run_details_does_not_show_waiting_validation_note_when_val_completed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-val-note-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-val-note-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 2,
            "usage": {"tests": 2},
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                "",
                "pass-fail",
                "running",
                json.dumps(summary),
                str(run_root),
                "2026-03-04T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                    }
                ),
                "2026-03-04T00:00:01Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generate Outputs", html)
        self.assertIn("Completed", html)
        self.assertNotIn("Waiting for validation results.", html)

    def test_run_details_cancel_during_step2_keeps_failure_on_generate_outputs(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-step2-cancel-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-step2-cancel-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step2-cancel")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        (build_root / "logs").mkdir(parents=True, exist_ok=True)
        (build_root / "logs" / "tests_meta.json").write_text(
            json.dumps(
                [
                    {"index": 1, "kind": "manual", "id": "m1", "sample": False},
                    {"index": 2, "kind": "gen", "id": "g2", "sample": False},
                ]
            ),
            encoding="utf-8",
        )
        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "running",
                "{}",
                str(build_root),
                "2026-03-05T00:00:00Z",
                "",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "running",
                        "error": "verification cancelled by user",
                        "cancelled": True,
                    }
                ),
                "2026-03-05T00:00:01Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generate Inputs", html)
        self.assertIn("Generate Outputs", html)
        self.assertIn("verification cancelled by user", html)
        # Step 1 should stay completed and Step 2 should be marked failed.
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-1"[\s\S]*?verification-lifecycle-tab-status">Completed<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">Failed<', re.IGNORECASE),
        )

    def test_run_details_marks_run_solutions_failed_when_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-run-failed-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-run-failed-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-run-failed")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "accepted solution failed on 001.in",
            "tests": [{"test": "001.in", "verdict": "FL"}],
            "tests_total": 1,
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": True,
                "passed_all_tests": False,
                "reason": "accepted solution failed on 001.in",
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:02Z",
                "2026-02-23T00:00:03Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "ok",
                        "error": "accepted solution failed on 001.in",
                    }
                ),
                "2026-02-23T00:00:04Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Run Solutions", html)
        self.assertIn("Failed", html)
        self.assertIn("failed (1/1 completed)", html)
        self.assertNotIn("1/1 solutions finished", html)

    def test_run_details_marks_run_solutions_interrupted_when_cancelled(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-run-cancel-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-run-cancel-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-run-cancel")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "ok",
                "{}",
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "verification cancelled by user",
            "cancelled": True,
            "tests": [],
            "tests_total": 0,
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:02Z",
                "2026-02-23T00:00:03Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "ok",
                        "error": "verification cancelled by user",
                    }
                ),
                "2026-02-23T00:00:04Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.cancel",
                json.dumps(
                    {
                        "invocation_id": invocation_id,
                        "run_ids": [run_id],
                        "reason": "verification cancelled by user",
                    }
                ),
                "2026-02-23T00:00:05Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Run Solutions", html)
        self.assertIn("Failed", html)
        self.assertIn("failed (1/1 completed)", html)
        self.assertIn("Cancelled solutions", html)
        self.assertNotIn("Failed solutions", html)
        self.assertNotIn("1/1 solutions finished", html)

    def test_run_details_verification_stays_on_step1_before_build_status_and_runs_exist(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-step1-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-step1-{uuid.uuid4().hex[:8]}"

        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                    }
                ),
                "2026-02-23T00:00:03Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("Generate Inputs", html)
        self.assertNotIn("failed (", html)

    def test_run_details_last_updated_is_empty_for_missing_runs_without_summary_scope(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-last-updated-{uuid.uuid4().hex[:8]}"
        run_id = f"r-last-updated-{uuid.uuid4().hex[:8]}"
        created_at = "2026-03-03T12:34:56+00:00"
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                    }
                ),
                created_at,
            ],
        )
        scope_run_ids = workspace_impl.run_invocation_scope_run_ids(
            problem_id,
            workspace_id,
            invocation_id,
        )
        self.assertEqual(scope_run_ids, [])
        detail_ctx = workspace_impl.build_run_detail_context(
            ctx,
            scope_run_ids,
            "pass-fail",
        )
        self.assertEqual(str(detail_ctx.get("detail_last_updated") or ""), "")

    def test_run_details_marks_build_failed_verification_execution_as_skipped(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-skip-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-skip-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-skip")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        db.execute(
            """
            INSERT INTO builds(id,problem_id,workspace_id,source_commit,source_ref,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                build_id,
                problem_id,
                workspace_id,
                "deadbeef",
                "main",
                "failed",
                json.dumps({"error": "compare script 173 crashed with exit code 1", "failed_step": "solve"}),
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "build failed: compare script 173 crashed with exit code 1",
            "tests": [],
            "tests_total": 0,
            "failure_stage": "build",
            "execution_skipped": True,
            "execution_skipped_reason": "build failed: compare script 173 crashed with exit code 1",
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "accepted solution must pass all tests",
                "execution_skipped": True,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:02Z",
                "2026-02-23T00:00:03Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                        "build_id": build_id,
                        "build_status": "failed",
                        "build_failed_step": "solve",
                        "error": "build failed: compare script 173 crashed with exit code 1",
                    }
                ),
                "2026-02-23T00:00:04Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Execution Status", html)
        self.assertIn("Run Solutions", html)
        self.assertRegex(html, r"build failed:\s*compare script\s+\d+\s+crashed with exit code\s+1")
        self.assertIn("Check Expectations", html)
        self.assertIn("Skipped", html)
        self.assertNotIn("expected match", html)
        self.assertIn("Verification Progress", html)
        self.assertNotIn("1/1 test reported", html)

    def test_run_details_check_notes_dedup_first_unmatched_error(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "zyc-2.py").write_text("print(0)\n", encoding="utf-8")
        (ws / "solutions" / "zyc.py").write_text("print(1)\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-verif-dedup-{uuid.uuid4().hex[:8]}"
        run_a = f"r-verif-dedup-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-verif-dedup-b-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-dedup")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)

        summary_a = {
            "mode": "pass-fail",
            "source": "solutions/zyc-2.py",
            "tests": [{"test": "001.in", "verdict": "WA"}],
            "invocation": {"id": invocation_id, "source": "verification.start", "run_ids": [run_a, run_b], "expected_behavior": "accepted", "matched": False, "completed": True, "passed_all_tests": False},
        }
        summary_b = {
            "mode": "pass-fail",
            "source": "solutions/zyc.py",
            "tests": [{"test": "001.in", "verdict": "WA"}],
            "invocation": {"id": invocation_id, "source": "verification.start", "run_ids": [run_a, run_b], "expected_behavior": "accepted", "matched": False, "completed": True, "passed_all_tests": False},
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_a,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_a),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_a),
                "2026-02-23T00:00:02Z",
                "2026-02-23T00:00:03Z",
            ],
        )
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_b,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary_b),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_b),
                "2026-02-23T00:00:04Z",
                "2026-02-23T00:00:05Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "invocation_id": invocation_id,
                        "run_id": run_a,
                        "run_ids": [run_a, run_b],
                        "run_count": 2,
                        "build_id": build_id,
                        "build_status": "ok",
                        "error": "zyc-2.py: required=[AC], allowed=[AC], got=[WA]",
                        "solutions": [
                            {"source_path": "solutions/zyc-2.py", "expected_behavior": "accepted", "run_id": run_a, "run_status": "ok", "completed": True, "passed_all_tests": False, "matched": False, "reason": "required=[AC], allowed=[AC], got=[WA]", "error": ""},
                            {"source_path": "solutions/zyc.py", "expected_behavior": "accepted", "run_id": run_b, "run_status": "ok", "completed": True, "passed_all_tests": False, "matched": False, "reason": "required=[AC], allowed=[AC], got=[WA]", "error": ""},
                        ],
                    }
                ),
                "2026-02-23T00:00:06Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("expected accepted (AC)", html)
        self.assertRegex(html, r"zyc\.py:[^<]*WA")
        self.assertRegex(html, r"zyc-2\.py:[^<]*WA")

    def test_run_details_uses_default_sidebar_without_detail_table(self) -> None:
        page = run_details_page(_request("/problems/alice/sample/alice/run/details"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No verification selected.", html)
        self.assertNotIn("page-grid-wide", html)

    def test_run_detail_compact_layout_heuristic_avoids_short_nine_column_table(self) -> None:
        short_columns = [{"title": f"s{i}.py"} for i in range(9)]
        self.assertFalse(run_export_impl._run_detail_use_compact_layout({"detail_columns": short_columns}))
        long_columns = [{"title": ("very-long-solution-name-" + ("x" * 30))} for _ in range(9)]
        self.assertTrue(run_export_impl._run_detail_use_compact_layout({"detail_columns": long_columns}))
        many_columns = [{"title": f"s{i}.py"} for i in range(12)]
        self.assertTrue(run_export_impl._run_detail_use_compact_layout({"detail_columns": many_columns}))

    def test_run_details_code_header_links_to_source_editor(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-source-link-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-source-link"),
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/alice/sample/alice/solutions/editor?path=solutions%2Faccepted.cpp", html)
        self.assertIn('class="invocation-col-title-link"><span class="invocation-col-title">accepted.cpp</span></a>', html)
        self.assertNotIn(f"/problems/alice/sample/alice/run/details?run_id={run_id}", html)

    def test_run_artifact_file_blocks_compile_log_download(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-ce-block-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "compile.log").write_text("compile error\n", encoding="utf-8")
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-ce-block"),
                "pass-fail",
                "failed",
                "{}",
                str(run_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        with self.assertRaises(HTTPException) as raised:
            run_export_impl.run_artifact_file("alice/sample", "alice", run_id, "compile.log")
        self.assertEqual(int(raised.exception.status_code), 403)

    def test_run_artifact_file_missing_redirects_with_rerun_hint(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-artifact-missing-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-artifact-missing"),
                "pass-fail",
                "ok",
                "{}",
                str(run_root),
                "2026-03-07T00:00:00Z",
                "2026-03-07T00:00:01Z",
            ],
        )
        resp = run_export_impl.run_artifact_file("alice/sample", "alice", run_id, "feedback_dir/001/judgemessage.txt")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(
            str(resp.headers.get("location") or ""),
            f"/problems/alice/sample/alice/run/details?run_id={run_id}",
        )
        flashes = _flash_messages_from_response(resp)
        self.assertTrue(any("rerun verification" in str(msg or "").lower() for msg in flashes))

    def test_run_artifact_file_serves_cache_blob_token(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-artifact-cache-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                int(ctx["workspace"]["id"]),
                self.random_id("b-artifact-cache"),
                "multi-pass",
                "ok",
                "{}",
                str(run_root),
                "2026-03-11T00:00:00Z",
                "2026-03-11T00:00:01Z",
            ],
        )
        service = config.judgehost_task_service
        key_hash = uuid.uuid4().hex + uuid.uuid4().hex
        signature = uuid.uuid4().hex + uuid.uuid4().hex
        service._domjudge_cache_put(
            service.CASE_CACHE_KIND,
            key_hash,
            signature,
            {"runresult": "correct"},
            files={"program.out": b"line 1\nline 2\n"},
            tags={"test": "cache-download"},
        )
        token = service._domjudge_cache_blob_ref(
            kind=service.CASE_CACHE_KIND,
            key_hash=key_hash,
            signature=signature,
            name="program.out",
        )

        resp = run_export_impl.run_artifact_file("alice/sample", "alice", run_id, token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body, b"line 1\nline 2\n")
        self.assertIn('attachment; filename="program.out"', str(resp.headers.get("content-disposition") or ""))
        self.assertEqual(str(resp.headers.get("content-type") or ""), "text/plain; charset=utf-8")

    def test_run_details_transcript_preview_shows_download_link(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        token = "cache://judgehost-domjudge-case/" + ("a" * 64) + "/" + ("b" * 64) + "/program.out"
        detail_ctx = {
            "detail_rows": [
                {
                    "test_name": "001.in",
                    "input_preview": {"available": False, "text": "", "truncated": False, "limit": 1024, "download_href": "", "message": "missing"},
                    "answer_preview": {"available": False, "text": "", "truncated": False, "limit": 1024, "download_href": "", "message": "missing"},
                    "cells": [
                        {
                            "detail": {
                                "final_row": {
                                    "kind": "ok",
                                    "verdict_short": "AC",
                                    "time_display": "1ms cpu, 2ms wall",
                                    "memory_display": "1MB",
                                    "feedback_display": "ok",
                                    "output_preview": {
                                        "available": True,
                                        "text": "> ping\n< pong\n",
                                        "truncated": False,
                                        "limit": 1024,
                                        "download_href": f"/problems/alice/sample/alice/runs/r-transcript/artifacts/{quote_plus(token)}",
                                        "message": "",
                                    },
                                    "interactive_transcript": {
                                        "available": True,
                                        "shown": 2,
                                        "rows": [{"side": "right", "text": "ping"}, {"side": "left", "text": "pong"}],
                                        "truncated": False,
                                    },
                                }
                            }
                        }
                    ],
                }
            ],
            "detail_columns": [{"id": "r-transcript", "title": "wtf.py"}],
        }

        with patch("app.impl.run_export.run.build_run_detail_context", return_value=detail_ctx):
            detail = run_details_test_fragment(
                _request("/problems/alice/sample/alice/run/details/test-fragment", "run_id=r-transcript&test=001.in"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("Transcript (first 2 lines)", detail_html)
        self.assertIn(f"/problems/alice/sample/alice/runs/r-transcript/artifacts/{quote_plus(token)}", detail_html)
        self.assertIn(">download</a>", detail_html)

    def test_run_cell_kind_nonaccepted_expected_uses_required_allowed_policy(self) -> None:
        self.assertEqual(workspace_impl._run_cell_kind("OK", "wrong_answer"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "run_time_error"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "run_time_error"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "run_time_error"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "time_limit_exceeded"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "time_limit_exceeded"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "time_limit_exceeded"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "tle_or_correct"), "ok")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_correct"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_correct"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_re"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("RE", "tle_or_re"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_re"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("AC", "tle_or_re"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "unknown"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "rejected"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "accepted"), "ok")

    def test_verification_match_uses_failed_status_set_for_tl_and_rejected(self) -> None:
        mixed_tl_re = {
            "tests": [
                {"test": "001.in", "verdict": "TL"},
                {"test": "002.in", "verdict": "RE"},
            ]
        }
        matched, completed, _observed_pass, reason = workspace_impl._verification_solution_match(
            "time_limit_exceeded",
            "ok",
            mixed_tl_re,
        )
        self.assertTrue(completed)
        self.assertFalse(matched)
        self.assertIn("required=[TL], allowed=[AC, TL], got=[TL, RE]", reason)

        ac_only = {
            "tests": [
                {"test": "001.in", "verdict": "OK"},
            ]
        }
        tl_ac_matched, tl_ac_completed, _tl_ac_pass, tl_ac_reason = workspace_impl._verification_solution_match(
            "time_limit_exceeded",
            "ok",
            ac_only,
        )
        self.assertTrue(tl_ac_completed)
        self.assertFalse(tl_ac_matched)
        self.assertIn("required=[TL], allowed=[AC, TL], got=[AC]", tl_ac_reason)

        tlac_matched, tlac_completed, _tlac_pass, tlac_reason = workspace_impl._verification_solution_match(
            "tle_or_correct",
            "ok",
            mixed_tl_re,
        )
        self.assertTrue(tlac_completed)
        self.assertFalse(tlac_matched)
        self.assertIn("required=[], allowed=[AC, TL], got=[TL, RE]", tlac_reason)

        tlre_matched, tlre_completed, _tlre_pass, tlre_reason = workspace_impl._verification_solution_match(
            "tle_or_re",
            "ok",
            mixed_tl_re,
        )
        self.assertTrue(tlre_completed)
        self.assertTrue(tlre_matched)
        self.assertEqual(tlre_reason, "")

        rejected_nonac = {
            "tests": [
                {"test": "001.in", "verdict": "WA"},
                {"test": "002.in", "verdict": "TL"},
            ]
        }
        rej_matched, rej_completed, _rej_pass, rej_reason = workspace_impl._verification_solution_match(
            "rejected",
            "ok",
            rejected_nonac,
        )
        self.assertTrue(rej_completed)
        self.assertTrue(rej_matched)
        self.assertEqual(rej_reason, "")

        rejected_all_ac = {
            "tests": [
                {"test": "001.in", "verdict": "OK"},
            ]
        }
        rej2_matched, _rej2_completed, _rej2_pass, rej2_reason = workspace_impl._verification_solution_match(
            "rejected",
            "ok",
            rejected_all_ac,
        )
        self.assertFalse(rej2_matched)
        self.assertIn("required=[WA, TL, RE, CE], allowed=[AC, WA, TL, RE, CE], got=[AC]", rej2_reason)

        rejected_fail = {
            "tests": [
                {"test": "001.in", "verdict": "FL"},
            ]
        }
        rej_fail_matched, rej_fail_completed, _rej_fail_pass, rej_fail_reason = workspace_impl._verification_solution_match(
            "rejected",
            "ok",
            rejected_fail,
        )
        self.assertTrue(rej_fail_completed)
        self.assertFalse(rej_fail_matched)
        self.assertIn("required=[WA, TL, RE, CE], allowed=[AC, WA, TL, RE, CE], got=[FL]", rej_fail_reason)

        unknown_fail_matched, unknown_fail_completed, _unknown_fail_pass, unknown_fail_reason = workspace_impl._verification_solution_match(
            "unknown",
            "ok",
            rejected_fail,
        )
        self.assertTrue(unknown_fail_completed)
        self.assertFalse(unknown_fail_matched)
        self.assertIn("required=[], allowed=[AC, WA, TL, RE, CE], got=[FL]", unknown_fail_reason)

    def test_run_details_marks_unknown_fail_as_unexpected_danger(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-unknown-fl-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-unknown-fl")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/custom.cpp",
            "tests": [
                {"test": "001.in", "verdict": "FL", "time_ms": 9, "memory_kb": 64},
            ],
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Expected match:", html)
        self.assertNotIn("unexpected custom.cpp", html)
        self.assertIn('<span class="danger">FL</span>', html)
        self.assertIn("invocation-cell-fail", html)

    def test_run_details_marks_ce_as_danger_even_when_expected_rejected(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-rejected-ce-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-rejected-ce")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/rejected.cpp",
            "tests": [
                {"test": "001.in", "verdict": "CE", "time_ms": 0, "memory_kb": 0},
            ],
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-03-03T00:00:00Z",
                "2026-03-03T00:00:01Z",
            ],
        )
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('<span class="danger">CE</span>', html)
        self.assertIn("invocation-cell-fail", html)

    def test_run_details_falls_back_to_verification_expected_behavior_for_cell_colors(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa_case.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-wa-fallback-{uuid.uuid4().hex[:8]}"
        run_id = f"r-wa-fallback-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-wa-fallback")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/wa_case.cpp",
            "tests": [
                {"test": "001.in", "verdict": "WA", "time_ms": 7, "memory_kb": 128},
                {"test": "002.in", "verdict": "OK", "time_ms": 5, "memory_kb": 96},
            ],
            "invocation": {
                "id": invocation_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "unknown",
                "completed": True,
            },
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-03-02T00:00:00Z",
                "2026-03-02T00:00:01Z",
            ],
        )
        db.execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "completed",
                        "invocation_id": invocation_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "solutions": [
                            {
                                "source_path": "solutions/wa_case.cpp",
                                "expected_behavior": "wrong_answer",
                                "run_id": run_id,
                                "matched": True,
                            }
                        ],
                    }
                ),
                "2026-03-02T00:00:02Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("invocation-cell-expected-nonac", html)
        self.assertIn("invocation-cell-neutral", html)
        self.assertNotIn("invocation-cell-ok", html)

    def test_run_details_shows_final_multi_pass_row_without_pass_column(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-multipass-rows-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-multipass-rows")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "multi-pass",
            "source": "solutions/multipass.cpp",
            "tests": [
                {
                    "test": "001.in",
                    "verdict": "WA",
                    "time_ms": 123,
                    "memory_kb": 512,
                    "passes": [
                        {"pass": 1, "verdict": "OK", "time_ms": 11, "time_user_ms": 7, "time_wall_ms": 15, "memory_kb": 256},
                        {"pass": 2, "verdict": "WA", "time_ms": 22, "time_user_ms": 19, "time_wall_ms": 34, "memory_kb": 512},
                    ],
                }
            ],
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                workspace_id,
                build_id,
                "multi-pass",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotRegex(html, r"<th[^>]*>Pass</th>")
        self.assertNotIn("<th>Sandbox</th>", html)
        self.assertNotIn(">P1</td>", html)
        self.assertNotIn(">P2</td>", html)
        self.assertNotRegex(html, r">7\s*ms cpu,\s*15\s*ms wall</td>")
        self.assertNotRegex(html, r">19\s*ms cpu,\s*34\s*ms wall</td>")

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"run_id={run_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertNotRegex(detail_html, r"<th[^>]*>Pass</th>")
        self.assertNotRegex(detail_html, r">7\s*ms cpu,\s*15\s*ms wall</td>")
        self.assertRegex(detail_html, r">19\s*ms cpu,\s*34\s*ms wall<")

    def test_run_details_prefers_workspace_answer_and_feedback_file_content(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        (ws / "tests" / "answers").mkdir(parents=True, exist_ok=True)
        (ws / "tests" / "answers" / "001.ans").write_text("37\n", encoding="utf-8")

        run_id = f"r-detail-answer-feedback-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-detail-answer-feedback")
        build_root = Path(str(config.settings.artifacts_root)) / "alice/sample" / build_id
        (build_root / "tests").mkdir(parents=True, exist_ok=True)
        (build_root / "ans").mkdir(parents=True, exist_ok=True)
        (build_root / "tests" / "001.in").write_text("1 1 123\n", encoding="utf-8")
        (build_root / "ans" / "001.ans").write_text("[  0.071s/0]]\n", encoding="utf-8")

        run_root = config.fs_manager.prepare_run_root(run_id).resolve()
        (run_root / "feedback_dir" / "001").mkdir(parents=True, exist_ok=True)
        judge_message = "Unexpected end of file - double expected\n"
        (run_root / "feedback_dir" / "001" / "judgemessage.txt").write_text(judge_message, encoding="utf-8")
        summary = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": [
                {
                    "test": "001.in",
                    "verdict": "WA",
                    "time_ms": 5,
                    "time_user_ms": 5,
                    "time_wall_ms": 50,
                    "memory_kb": 1024,
                    "passes": [
                        {
                            "pass": 1,
                            "verdict": "WA",
                            "time_ms": 5,
                            "time_user_ms": 5,
                            "time_wall_ms": 50,
                            "memory_kb": 1024,
                        }
                    ],
                    "feedback_files": ["feedback_dir/001/judgemessage.txt"],
                }
            ],
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                int(ctx["problem"]["id"]),
                workspace_id,
                build_id,
                "pass-fail",
                "ok",
                json.dumps(summary),
                str(run_root),
                "2026-03-03T00:00:00Z",
                "2026-03-03T00:00:01Z",
            ],
        )

        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("data-run-details-fragment", html)
        self.assertNotIn("Unexpected end of file - double expected", html)
        self.assertNotIn("[  0.071s/0]]", html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"run_id={run_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertRegex(detail_html, r"(?s)<strong>Answer</strong>.*?<pre[^>]*>\s*37\s*</pre>")
        self.assertNotIn("[  0.071s/0]]", detail_html)
        self.assertIn(judge_message.strip(), detail_html)
        self.assertNotIn("feedback_dir/001/judgemessage.txt", detail_html)

    def test_async_run_failure_shows_fl_reason_in_test_details(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-async-fail")
        build_root = self._artifact_root(build_id)
        build_root.mkdir(parents=True, exist_ok=True)
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
                "failed",
                json.dumps(
                    {
                        "error": "accepted solution failed on 001.in",
                        "failed_step": "solve",
                        "failed_test": "001.in",
                    }
                ),
                str(build_root),
                "2026-02-23T00:00:00Z",
                "2026-02-23T00:00:01Z",
            ],
        )

        run_id = f"r-async-fail-{uuid.uuid4().hex[:8]}"
        workspace_impl.record_async_run_failure(
            "alice/sample",
            "alice",
            run_id,
            mode="pass-fail",
            source_label="solutions/jly.cpp",
            error=f"build not runnable: {build_id}",
            build_id=build_id,
        )

        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"run_id={run_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("No per-test details yet.", html)
        self.assertIn("001.in", html)
        self.assertIn('invocation-cell-code">FL</span>', html)
        self.assertNotIn("accepted solution failed on 001.in", html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"run_id={run_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("accepted solution failed on 001.in", detail_html)

    def test_workflow_pages_emit_files_source_context_links(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        ws = Path(str(ctx["workspace"]["path"]))
        statement_sig = statement_sources_signature(ws, problem_title=str(ctx["problem"]["name"]))

        preview_id = f"ui-previewctx-{uuid.uuid4().hex[:8]}"
        preview_root = self._artifact_root(preview_id)
        (preview_root / "logs").mkdir(parents=True, exist_ok=True)
        (preview_root / "logs" / "latex.log").write_text("statement/main.tex:7 Undefined control sequence\n", encoding="utf-8")
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
                "2026-02-23T00:01:00Z",
                "2026-02-23T00:01:01Z",
            ],
        )
        preview_resp = preview_page(_request("/problems/alice/sample/alice/preview", f"preview_id={preview_id}"), "alice/sample", "alice")
        preview_html = preview_resp.body.decode("utf-8", errors="replace")
        self.assertIn("src=statement", preview_html)
        self.assertIn(f"sid={preview_id}", preview_html)
        self.assertNotIn("2026-02-23T00:01:00Z", preview_html)

        run_id = f"ui-runctx-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("ui-rundetail-build")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        run_summary = {
            "source": "solutions/accepted.cpp",
            "compile_diagnostics": [
                {
                    "level": "error",
                    "file": "solutions/accepted.cpp",
                    "line": 12,
                    "column": 4,
                    "message": "compile failed",
                    "can_link": True,
                }
            ]
        }
        db.execute(
            """
            INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                run_id,
                problem_id,
                workspace_id,
                build_id,
                "pass-fail",
                "failed",
                json.dumps(run_summary),
                str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id),
                "2026-02-23T00:02:00Z",
                "2026-02-23T00:02:01Z",
            ],
        )
        run_resp = run_page(_request("/problems/alice/sample/alice/run", f"run_id={run_id}"), "alice/sample", "alice")
        run_html = run_resp.body.decode("utf-8", errors="replace")
        self.assertIn("/solutions/editor?path=solutions%2Faccepted.cpp", run_html)



