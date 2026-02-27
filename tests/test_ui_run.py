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
    db,
    general_page,
    io,
    json,
    os,
    parse_qs,
    patch,
    preview_page,
    run_details_page,
    run_execute,
    run_export_impl,
    run_new_page,
    run_page,
    run_service,
    statement_sources_signature,
    tests_spec_add_gen,
    tests_spec_add_gen_batch,
    tests_spec_add_manual,
    tests_spec_add_manual_batch,
    tests_spec_delete,
    tests_spec_move,
    tests_spec_payload_download,
    tests_spec_payload_upload,
    tests_spec_update,
    threading,
    time,
    urlparse,
    uuid,
    verification_start,
    workspace_impl,
    workspace_service,
)


class TestUIRun(UIBaseSuite):
    def test_tests_spec_crud_updates_spec_file_and_page(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
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
            problem="sample",
            user="alice",
            test_id="001",
            manual_input="1 2 3  \r\n4 5\t \r\n",
        )
        self.assertEqual(add_manual.status_code, 303)
        self.assertIn("/problems/sample/alice/tests", add_manual.headers.get("location", ""))

        add_gen = tests_spec_add_gen(
            problem="sample",
            user="alice",
            test_id="002",
            command="gen 10 20",
        )
        self.assertEqual(add_gen.status_code, 303)

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

        update_gen = tests_spec_update(
            problem="sample",
            user="alice",
            index="2",
            kind="gen",
            sample="1",
            command="gen 99",
        )
        self.assertEqual(update_gen.status_code, 303)
        self.assertEqual((generator_dir / "002.in").read_text(encoding="utf-8"), "gen 99")

        move_up = tests_spec_move(
            problem="sample",
            user="alice",
            index="2",
            direction="up",
        )
        self.assertEqual(move_up.status_code, 303)

        delete_second = tests_spec_delete(
            problem="sample",
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

        page = build_page(_request("/problems/sample/alice/tests"), "sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests/spec.json", html)
        self.assertIn("tests/generator/002.in", html)
        self.assertIn("gen 99", html)

    def test_tests_spec_batch_add_routes(self) -> None:
        ws_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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

        add_manual_batch = tests_spec_add_manual_batch(
            problem="sample",
            user="alice",
            desc_prefix="manual-batch",
            manual_batch_text="1 2\n---\n3 4\n===\n5 6\n",
        )
        self.assertEqual(add_manual_batch.status_code, 303)

        add_gen_batch = tests_spec_add_gen_batch(
            problem="sample",
            user="alice",
            desc_prefix="gen-batch",
            gen_batch_text="gen 10 1\ngen 20 2\n",
        )
        self.assertEqual(add_gen_batch.status_code, 303)

        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        tests = payload.get("tests") or []
        self.assertEqual(len(tests), 5)
        self.assertEqual(tests[0].get("kind"), "manual")
        self.assertEqual(tests[0].get("id"), "001")
        self.assertEqual(tests[1].get("id"), "002")
        self.assertEqual(tests[2].get("id"), "003")
        self.assertEqual(tests[3].get("kind"), "gen")
        self.assertEqual(tests[3].get("id"), "004")
        self.assertEqual(tests[4].get("id"), "005")
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "1 2\n")
        self.assertEqual((manual_dir / "002.in").read_text(encoding="utf-8"), "3 4\n")
        self.assertEqual((manual_dir / "003.in").read_text(encoding="utf-8"), "5 6\n")
        self.assertEqual((generator_dir / "004.in").read_text(encoding="utf-8"), "gen 10 1")
        self.assertEqual((generator_dir / "005.in").read_text(encoding="utf-8"), "gen 20 2")

    def test_tests_spec_large_manual_disables_inline_editor_and_shows_payload_actions(self) -> None:
        ws_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
            problem="sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        huge_manual = ("A" * 200000) + "\n"
        (manual_dir / "001.in").write_text(huge_manual, encoding="utf-8")

        page = build_page(_request("/problems/sample/alice/tests"), "sample", "alice")
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

        ws_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
            problem="sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        upload_payload = _FakeUpload(b"7 8 9  \r\n10 11\t \r\n")
        uploaded = asyncio.run(
            tests_spec_payload_upload(
                problem="sample",
                user="alice",
                index="1",
                payload_upload=upload_payload,
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("/problems/sample/alice/tests", uploaded.headers.get("location", ""))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "7 8 9\n10 11\n")

        downloaded = tests_spec_payload_download(problem="sample", user="alice", index="1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("001.in", str(downloaded.headers.get("content-disposition", "")))

    def test_run_execute_without_tests_triggers_implicit_tests_generation(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        ws = Path(str(ctx["workspace"]["path"]))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        workspace_id = int(ctx["workspace"]["id"])
        db.execute("DELETE FROM runs WHERE workspace_id=?", [workspace_id])
        db.execute("DELETE FROM builds WHERE workspace_id=?", [workspace_id])

        resp = run_execute(
            problem="sample",
            user="alice",
            build_id="",
            solution_paths=["solutions/accepted.cpp"],
            submission_upload=None,
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/run/details?invocation_id=", loc)
        run_messages = _flash_messages_from_response(resp)
        self.assertTrue(run_messages)
        self.assertIn("invocation running", run_messages[0])
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
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        problem_cfg = ws / "config" / "problem.json"
        problem_cfg.parent.mkdir(parents=True, exist_ok=True)
        problem_cfg.write_text(json.dumps({"mode": "multi-pass"}, indent=2) + "\n", encoding="utf-8")

        resp = run_execute(
            problem="sample",
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
            [int(workspace_service.workspace_context("sample", "alice", include_recent=False)["workspace"]["id"]), f"%{invocation_id}%"],
            timeout_sec=8.0,
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["mode"]), "multi-pass")

    def test_run_execute_upload_compile_check_blocks_invalid_python(self) -> None:
        class _FakeSubmission:
            def __init__(self, name: str, payload: bytes):
                self.filename = name
                self.file = io.BytesIO(payload)

        with patch("app.impl.run_export._start_run_execute_batch") as start_batch:
            resp = run_execute(
                problem="sample",
                user="alice",
                build_id="",
                solution_paths=[],
                submission_upload=_FakeSubmission("broken.py", b"def broken(:\n    pass\n"),
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertEqual(loc, "/problems/sample/alice/run/new")
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        message = messages[0]
        self.assertIn("upload compile check failed", message)
        self.assertTrue(("syntax" in message.lower()) or ("error" in message.lower()))
        start_batch.assert_not_called()

    def test_run_execute_upload_compile_check_allows_valid_python(self) -> None:
        class _FakeSubmission:
            def __init__(self, name: str, payload: bytes):
                self.filename = name
                self.file = io.BytesIO(payload)

        with patch("app.impl.run_export._start_run_execute_batch") as start_batch:
            resp = run_execute(
                problem="sample",
                user="alice",
                build_id="",
                solution_paths=[],
                submission_upload=_FakeSubmission("ok.py", b"print('ok')\n"),
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/run/details?invocation_id=", loc)
        start_batch.assert_called_once()
        kwargs = start_batch.call_args.kwargs
        targets = kwargs.get("targets")
        self.assertIsInstance(targets, list)
        self.assertTrue(targets)
        first = targets[0]
        self.assertEqual(str(first.get("upload_filename") or ""), "ok.py")
        self.assertIsInstance(first.get("upload_content"), (bytes, bytearray))

    def test_run_execute_records_invocation_audit_before_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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

        with patch("app.impl.run_export._start_run_execute_batch", side_effect=_fake_start_batch):
            resp = run_execute(
                problem="sample",
                user="alice",
                build_id="",
                solution_paths=["solutions/accepted.cpp", "solutions/wa.cpp"],
                submission_upload=None,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertTrue(observed["checked"])
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/sample/alice/run/details?invocation_id=", loc)
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

    def test_run_execute_solution_compile_check_blocks_invalid_saved_source(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        broken = ws / "solutions" / "broken.cpp"
        broken.write_text("int main( { return 0; }\n", encoding="utf-8")

        with patch("app.impl.run_export._start_run_execute_batch") as start_batch:
            resp = run_execute(
                problem="sample",
                user="alice",
                build_id="",
                solution_paths=["solutions/broken.cpp"],
                submission_upload=None,
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertEqual(loc, "/problems/sample/alice/run/new")
        messages = _flash_messages_from_response(resp)
        self.assertTrue(messages)
        message = messages[0]
        self.assertIn("compile check failed", message)
        self.assertIn("solutions/broken.cpp", message)
        start_batch.assert_not_called()

    def test_run_execute_passes_selected_tests_to_runner(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ws_ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ws_ctx["problem"]["id"])
        workspace_id = int(ws_ctx["workspace"]["id"])

        build_id = f"b-select-tests-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id
        build_artifact.mkdir(parents=True, exist_ok=True)
        (build_artifact / "tests").mkdir(parents=True, exist_ok=True)
        (build_artifact / "ans").mkdir(parents=True, exist_ok=True)

        def _fake_run_build(_problem: str, _user: str) -> str:
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
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        captured_selected_tests: list[list[str]] = []

        def _fake_run_submission(
            _problem: str,
            _user: str,
            _build_id: str,
            submission_path: str | None = None,
            mode: str = "pass-fail",
            upload_content: bytes | None = None,
            upload_filename: str | None = None,
            upload_stream=None,
            run_id: str | None = None,
            selected_tests: list[str] | None = None,
            invocation_id: str | None = None,
            invocation_run_ids: list[str] | None = None,
            expected_behavior: str | None = None,
            invocation_source: str = "run.execute",
        ) -> str:
            effective_run_id = str(run_id or f"r-select-tests-{uuid.uuid4().hex[:8]}")
            selected = [str(item or "") for item in (selected_tests or []) if str(item or "")]
            captured_selected_tests.append(selected)
            tests = selected or ["001.in"]
            run_root = build_artifact / "logs" / f"run-{effective_run_id}"
            run_root.mkdir(parents=True, exist_ok=True)
            summary = {
                "mode": mode,
                "source": submission_path or "solutions/accepted.cpp",
                "tests": [{"test": name, "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]} for name in tests],
            }
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    effective_run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    mode,
                    "ok",
                    json.dumps(summary),
                    str(run_root),
                    "2026-02-23T00:00:02Z",
                    "2026-02-23T00:00:03Z",
                ],
            )
            return effective_run_id

        original_run_build = build_service.run_build
        original_run_submission = run_service.run_submission
        build_service.run_build = _fake_run_build
        run_service.run_submission = _fake_run_submission
        try:
            resp = run_execute(
                problem="sample",
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
            _wait_for_run_execute_workers(timeout_sec=8.0)
        finally:
            build_service.run_build = original_run_build
            run_service.run_submission = original_run_submission

        self.assertEqual(captured_selected_tests, [["001.in", "003.in"]])

    def test_verification_start_requires_main_correct_solution_marker(self) -> None:
        problem = f"verify-main-required-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        cfg_path = ws / "config" / "build.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.pop("accepted_solution_source", None)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        start_resp = verification_start(problem=problem, user="alice", page="general")
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

    def test_verification_start_updates_sidebar_status_to_pass(self) -> None:
        problem = f"verify-pass-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() { cout << 0 << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "")
        build_id = f"b-vpass-{uuid.uuid4().hex[:8]}"
        run_id = f"r-vpass-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        run_artifact = build_artifact / "logs" / f"run-{run_id}"
        build_artifact.mkdir(parents=True, exist_ok=True)
        run_artifact.mkdir(parents=True, exist_ok=True)

        def _fake_run_build(_problem: str, _user: str) -> str:
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
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        def _fake_run_submission(
            _problem: str,
            _user: str,
            _build_id: str,
            submission_path: str | None = None,
            mode: str = "pass-fail",
            upload_content: bytes | None = None,
            upload_filename: str | None = None,
            upload_stream=None,
            run_id: str | None = None,
            selected_tests: list[str] | None = None,
            invocation_id: str | None = None,
            invocation_run_ids: list[str] | None = None,
            expected_behavior: str | None = None,
            invocation_source: str = "run.execute",
        ) -> str:
            self.assertEqual(_build_id, build_id)
            effective_run_id = str(run_id or f"r-vpass-{uuid.uuid4().hex[:8]}")
            called_sources.append(str(submission_path or ""))
            verdict = "OK" if str(submission_path or "").endswith("accepted.cpp") else "WA"
            summary = {
                "mode": mode,
                "source": submission_path or "solutions/accepted.cpp",
                "tests": [{"test": "001.in", "verdict": verdict, "passes": [{"pass": 1, "verdict": verdict}]}],
            }
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    effective_run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    mode,
                    "ok",
                    json.dumps(summary),
                    str(run_artifact),
                    "2026-02-23T00:00:02Z",
                    "2026-02-23T00:00:03Z",
                ],
            )
            return effective_run_id

        original_run_build = build_service.run_build
        original_run_submission = run_service.run_submission
        called_sources: list[str] = []
        build_service.run_build = _fake_run_build
        run_service.run_submission = _fake_run_submission
        try:
            start_resp = verification_start(problem=problem, user="alice", page="general")
            _wait_for_verification_workers(timeout_sec=10.0)
        finally:
            build_service.run_build = original_run_build
            run_service.run_submission = original_run_submission

        self.assertEqual(start_resp.status_code, 303)
        loc = start_resp.headers.get("location", "")
        self.assertIn(f"/problems/{problem}/alice/general", loc)
        pass_messages = _flash_messages_from_response(start_resp)
        self.assertTrue(pass_messages)
        self.assertIn("verification running", pass_messages[0])
        self.assertEqual(sorted(called_sources), ["solutions/accepted.cpp", "solutions/wa.cpp"])

        page = general_page(_request(f"/problems/{problem}/alice/general"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(html, r'<span class="status-title(?: [^"]*)?">Verification</span>')
        self.assertIn(">pass</strong>", html)

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
        self.assertEqual(payload.get("status"), "pass")
        self.assertEqual(int(payload.get("run_count") or 0), 2)

    def test_verification_start_updates_sidebar_status_to_failed(self) -> None:
        problem = f"verify-fail-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"b-vfail-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_artifact.mkdir(parents=True, exist_ok=True)

        def _fake_run_build(_problem: str, _user: str) -> str:
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
                    json.dumps({"error": "compile failed"}),
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        original_run_build = build_service.run_build
        build_service.run_build = _fake_run_build
        try:
            start_resp = verification_start(problem=problem, user="alice", page="general")
            _wait_for_verification_workers(timeout_sec=10.0)
        finally:
            build_service.run_build = original_run_build

        self.assertEqual(start_resp.status_code, 303)
        loc = start_resp.headers.get("location", "")
        self.assertIn(f"/problems/{problem}/alice/general", loc)
        fail_messages = _flash_messages_from_response(start_resp)
        self.assertTrue(fail_messages)
        msg = fail_messages[0]
        self.assertIn("verification running", msg)
        self.assertNotIn(build_id, msg)

        page = general_page(_request(f"/problems/{problem}/alice/general"), problem, "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(">failed</strong>", html)
        self.assertRegex(
            html,
            r'<span class="status-title(?: [^"]+)?">Verification</span>\s*<strong\s+class="status-value danger"[^>]*>\s*failed</strong>',
        )
        self.assertRegex(
            html,
            r'<strong\s+class="status-value danger"[^>]*data-tooltip="[^"]*compile failed[^"]*"[^>]*>\s*failed</strong>',
        )
        self.assertIn("compile failed", html)
        self.assertNotIn(build_id, html)

    def test_verification_sidebar_marks_stale_when_gen_chk_sol_tests_change(self) -> None:
        problem = f"verify-stale-{uuid.uuid4().hex[:8]}"
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

        page = general_page(_request(f"/problems/{problem}/alice/general"), problem, "alice")
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
        problem = f"verify-stale-general-{uuid.uuid4().hex[:8]}"
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

        page = general_page(_request(f"/problems/{problem}/alice/general"), problem, "alice")
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

    def test_verification_fails_when_nonaccepted_solution_passes(self) -> None:
        problem = f"verify-nonac-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() { long long x = 0; if (!(cin >> x)) return 0; cout << x << "\\n"; return 0; }
""",
            encoding="utf-8",
        )
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "")
        build_id = f"b-vnonac-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_artifact.mkdir(parents=True, exist_ok=True)

        def _fake_run_build(_problem: str, _user: str) -> str:
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
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        def _fake_run_submission(
            _problem: str,
            _user: str,
            _build_id: str,
            submission_path: str | None = None,
            mode: str = "pass-fail",
            upload_content: bytes | None = None,
            upload_filename: str | None = None,
            upload_stream=None,
            run_id: str | None = None,
            selected_tests: list[str] | None = None,
            invocation_id: str | None = None,
            invocation_run_ids: list[str] | None = None,
            expected_behavior: str | None = None,
            invocation_source: str = "run.execute",
        ) -> str:
            effective_run_id = str(run_id or f"r-vnonac-{uuid.uuid4().hex[:8]}")
            summary = {
                "mode": mode,
                "source": submission_path or "solutions/accepted.cpp",
                "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
            }
            run_root = build_artifact / "logs" / f"run-{effective_run_id}"
            run_root.mkdir(parents=True, exist_ok=True)
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    effective_run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    mode,
                    "ok",
                    json.dumps(summary),
                    str(run_root),
                    "2026-02-23T00:00:02Z",
                    "2026-02-23T00:00:03Z",
                ],
            )
            return effective_run_id

        original_run_build = build_service.run_build
        original_run_submission = run_service.run_submission
        build_service.run_build = _fake_run_build
        run_service.run_submission = _fake_run_submission
        try:
            start_resp = verification_start(problem=problem, user="alice", page="general")
            _wait_for_verification_workers(timeout_sec=10.0)
        finally:
            build_service.run_build = original_run_build
            run_service.run_submission = original_run_submission

        self.assertEqual(start_resp.status_code, 303)
        loc = start_resp.headers.get("location", "")
        self.assertIn(f"/problems/{problem}/alice/general", loc)
        fail_messages = _flash_messages_from_response(start_resp)
        self.assertTrue(fail_messages)
        self.assertIn("verification running", fail_messages[0])

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
        self.assertEqual(int(payload.get("run_count") or 0), 2)
        self.assertIn("expected WA, got AC", str(payload.get("error") or ""))

    def test_verification_fails_when_expected_wa_gets_tl(self) -> None:
        problem = f"verify-wa-tl-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text(
            """#include <bits/stdc++.h>
using namespace std;
int main() { for (;;) {} }
""",
            encoding="utf-8",
        )
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "")
        build_id = f"b-vwatl-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_artifact.mkdir(parents=True, exist_ok=True)

        def _fake_run_build(_problem: str, _user: str) -> str:
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
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        def _fake_run_submission(
            _problem: str,
            _user: str,
            _build_id: str,
            submission_path: str | None = None,
            mode: str = "pass-fail",
            upload_content: bytes | None = None,
            upload_filename: str | None = None,
            upload_stream=None,
            run_id: str | None = None,
            selected_tests: list[str] | None = None,
            invocation_id: str | None = None,
            invocation_run_ids: list[str] | None = None,
            expected_behavior: str | None = None,
            invocation_source: str = "run.execute",
        ) -> str:
            effective_run_id = str(run_id or f"r-vwatl-{uuid.uuid4().hex[:8]}")
            verdict = "OK" if str(submission_path or "").endswith("accepted.cpp") else "TL"
            summary = {
                "mode": mode,
                "source": submission_path or "solutions/accepted.cpp",
                "tests": [{"test": "001.in", "verdict": verdict, "passes": [{"pass": 1, "verdict": verdict}]}],
            }
            run_root = build_artifact / "logs" / f"run-{effective_run_id}"
            run_root.mkdir(parents=True, exist_ok=True)
            db.execute(
                """
                INSERT INTO runs(id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    effective_run_id,
                    problem_id,
                    workspace_id,
                    build_id,
                    mode,
                    "ok",
                    json.dumps(summary),
                    str(run_root),
                    "2026-02-23T00:00:02Z",
                    "2026-02-23T00:00:03Z",
                ],
            )
            return effective_run_id

        original_run_build = build_service.run_build
        original_run_submission = run_service.run_submission
        build_service.run_build = _fake_run_build
        run_service.run_submission = _fake_run_submission
        try:
            start_resp = verification_start(problem=problem, user="alice", page="general")
            _wait_for_verification_workers(timeout_sec=10.0)
        finally:
            build_service.run_build = original_run_build
            run_service.run_submission = original_run_submission

        self.assertEqual(start_resp.status_code, 303)
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
        self.assertIn("expected WA, got TL", str(payload.get("error") or ""))

    def test_verification_start_returns_immediately_while_worker_runs(self) -> None:
        problem = f"verify-async-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"b-vasync-{uuid.uuid4().hex[:8]}"
        build_artifact = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_artifact.mkdir(parents=True, exist_ok=True)
        started = threading.Event()
        release = threading.Event()

        def _fake_run_build(_problem: str, _user: str) -> str:
            started.set()
            release.wait(5.0)
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
                    json.dumps({"error": "compile failed"}),
                    str(build_artifact),
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return build_id

        original_run_build = build_service.run_build
        build_service.run_build = _fake_run_build
        try:
            started_at = time.monotonic()
            start_resp = verification_start(problem=problem, user="alice", page="general")
            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 1.0)

            self.assertEqual(start_resp.status_code, 303)
            running_messages = _flash_messages_from_response(start_resp)
            self.assertTrue(running_messages)
            self.assertIn("verification running", running_messages[0])

            running_row = db.fetch_one(
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
            self.assertIsNotNone(running_row)
            running_payload = json.loads(str(running_row["details_json"]))
            self.assertEqual(running_payload.get("status"), "running")
            self.assertGreaterEqual(int(running_payload.get("run_count") or 0), 1)
            run_ids_running = [str(item or "") for item in (running_payload.get("run_ids") or []) if str(item or "")]
            self.assertEqual(int(running_payload.get("run_count") or 0), len(run_ids_running))
            self.assertTrue(all(item.startswith("r-") for item in run_ids_running))
            self.assertTrue(str(running_payload.get("run_id") or "").startswith("r-"))

            self.assertTrue(started.wait(2.0))
            release.set()
            _wait_for_verification_workers(timeout_sec=10.0)
        finally:
            release.set()
            build_service.run_build = original_run_build

        done_row = db.fetch_one(
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
        self.assertIsNotNone(done_row)
        done_payload = json.loads(str(done_row["details_json"]))
        self.assertEqual(done_payload.get("status"), "failed")
        self.assertIn("build failed", str(done_payload.get("error") or ""))

    def test_run_page_shows_multi_solution_selector_without_mode_select(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")

        page = run_new_page(_request("/problems/sample/alice/run/new", "solution_paths=solutions/wa.cpp"), "sample", "alice")
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

    def test_run_page_defaults_all_tests_checked_when_available(self) -> None:
        problem = f"run-default-tests-{uuid.uuid4().hex[:8]}"
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
        page = run_page(_request("/problems/sample/alice/run"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No invocation yet.", html)
        self.assertNotIn("page-grid-wide", html)

    def test_run_list_groups_by_audit_mapping_when_summary_is_oversized(self) -> None:
        problem = f"inv-oversized-{uuid.uuid4().hex[:8]}"
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

        rows = workspace_impl._run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), invocation_id)
        self.assertEqual(int(rows[0].get("run_count") or 0), 2)

    def test_run_list_groups_verification_runs_from_initial_audit_run_ids(self) -> None:
        problem = f"inv-verify-map-{uuid.uuid4().hex[:8]}"
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

        rows = workspace_impl._run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), invocation_id)
        self.assertEqual(int(rows[0].get("run_count") or 0), 2)

    def test_run_details_tracks_invocation_scope_across_async_refreshes(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        invocation_id = f"inv-refresh-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-refresh-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-refresh-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-inv-refresh")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "sample" / build_id
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
            _request("/problems/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "sample",
            "alice",
        )
        self.assertEqual(first.status_code, 200)
        first_html = first.body.decode("utf-8", errors="replace")
        self.assertIn("Program 1", first_html)
        self.assertIn("Program 2", first_html)
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
            _request("/problems/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "sample",
            "alice",
        )
        self.assertEqual(second.status_code, 200)
        second_html = second.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", second_html)
        self.assertIn("Program 2", second_html)
        self.assertIn("Expected match: 1/2", second_html)

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
            _request("/problems/sample/alice/run/details", f"invocation_id={invocation_id}"),
            "sample",
            "alice",
        )
        self.assertEqual(third.status_code, 200)
        third_html = third.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", third_html)
        self.assertIn("wa.cpp", third_html)
        self.assertIn("Expected match: 2/2 (success)", third_html)

    def test_run_details_uses_default_sidebar_without_detail_table(self) -> None:
        page = run_details_page(_request("/problems/sample/alice/run/details"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No invocation selected.", html)
        self.assertNotIn("page-grid-wide", html)

    def test_run_details_code_header_links_to_source_editor(self) -> None:
        ws = Path(workspace_service.ensure_workspace("sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
        page = run_details_page(_request("/problems/sample/alice/run/details", f"run_id={run_id}"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/sample/alice/solutions/editor?path=solutions%2Faccepted.cpp", html)
        self.assertIn(">accepted.cpp</a>", html)
        self.assertNotIn(f"/problems/sample/alice/run/details?run_id={run_id}", html)

    def test_run_artifact_file_blocks_compile_log_download(self) -> None:
        workspace_service.ensure_workspace("sample", "alice")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
            run_export_impl.run_artifact_file("sample", "alice", run_id, "compile.log")
        self.assertEqual(int(raised.exception.status_code), 403)

    def test_run_cell_kind_nonaccepted_expected_treats_ac_as_neutral(self) -> None:
        self.assertEqual(workspace_impl._run_cell_kind("OK", "wrong_answer"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "run_time_error"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "run_time_error"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("OK", "time_limit_exceeded"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "time_limit_exceeded"), "expected-nonac")
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
        self.assertIn("expected TL, got TL/RE", reason)

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
        self.assertIn("expected rejected, got AC", rej2_reason)

    def test_run_details_shows_failed_status_set_for_expected_tl_mismatch(self) -> None:
        workspace_service.ensure_workspace("sample", "alice")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-tl-set-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-tl-set")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/tle_or_ac.py",
            "tests": [
                {"test": "001.in", "verdict": "TL", "time_ms": 9, "memory_kb": 64},
                {"test": "002.in", "verdict": "RE", "time_ms": 7, "memory_kb": 64},
                {"test": "003.in", "verdict": "WA", "time_ms": 5, "memory_kb": 64},
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
        page = run_details_page(_request("/problems/sample/alice/run/details", f"run_id={run_id}"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("expected TL, got", html)
        self.assertIn('<span class="danger">TL/RE/WA</span>', html)

    def test_run_details_lists_each_multi_pass_row(self) -> None:
        workspace_service.ensure_workspace("sample", "alice")
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
                        {"pass": 1, "verdict": "OK", "time_ms": 11, "memory_kb": 256},
                        {"pass": 2, "verdict": "WA", "time_ms": 22, "memory_kb": 512},
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
        page = run_details_page(_request("/problems/sample/alice/run/details", f"run_id={run_id}"), "sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(html, r"<th[^>]*>Pass</th>")
        self.assertNotIn("<th>Sandbox</th>", html)
        self.assertIn(">P1</td>", html)
        self.assertIn(">P2</td>", html)
        self.assertRegex(html, r">11\s*ms</td>")
        self.assertRegex(html, r">22\s*ms</td>")

    def test_workflow_pages_emit_files_source_context_links(self) -> None:
        ctx = workspace_service.workspace_context("sample", "alice", include_recent=False)
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
        preview_resp = preview_page(_request("/problems/sample/alice/preview", f"preview_id={preview_id}"), "sample", "alice")
        preview_html = preview_resp.body.decode("utf-8", errors="replace")
        self.assertIn("src=preview", preview_html)
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
        run_resp = run_page(_request("/problems/sample/alice/run", f"run_id={run_id}"), "sample", "alice")
        run_html = run_resp.body.decode("utf-8", errors="replace")
        self.assertIn("/solutions/editor?path=solutions%2Faccepted.cpp", run_html)
