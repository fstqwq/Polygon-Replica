from __future__ import annotations

from .db_helpers import db_execute, db_fetch_one

import base64
import asyncio
import io
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

from app.service.statement.signature import statement_sources_signature

from .ui_support import (
    Path,
    UIBaseSuite,
    _flash_messages_from_response,
    _request,
    _wait_for_row,
    tests_page as ui_tests_page,
    tests_spec_add_gen as add_gen_call,
    tests_spec_edit as edit_spec_call,
    tests_spec_add_manual as add_manual_call,
    tests_spec_add_manual_upload as add_manual_upload_call,
    tests_spec_delete as delete_spec_call,
    tests_spec_gen_script_save as save_gen_script_call,
    tests_spec_payload_download as download_payload_call,
    tests_spec_payload_upload as upload_payload_call,
    tests_spec_reindex as reindex_spec_call,
    config,
    general_page,
    json,
    preview_page,
    quote_plus,
    run_details_page,
    run_details_test_fragment,
    run_execute,
    run_export_impl,
    run_new_page,
    run_page,
    time,
    uuid,
    verification_start,
    workspace_impl,
    workspace_service,
)
import app.impl.workspace.context_job as workspace_context_job
import app.impl.workspace.context_verification as workspace_verification_module
from app.service.verification.types import Kind


class TestUIRun(UIBaseSuite):
    @staticmethod
    def _verification_id_for_run(run_id: str) -> str:
        return f"ver-{str(run_id or '').strip()}"

    def _insert_verification_row(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int,
        build_id: str,
        kind: str,
        status: str,
        created_at: str,
        finished_at: str,
        runs: list[dict[str, object]],
        summary_extra: dict[str, object] | None = None,
    ) -> None:
        verification_root = config.fs_manager.prepare_verification_root(verification_id).resolve()
        verification_root.mkdir(parents=True, exist_ok=True)
        existing_row = db_fetch_one(
            "SELECT summary_json,created_at,finished_at,source_commit,source_ref FROM verifications WHERE id=?",
            [verification_id],
        )
        existing_summary: dict[str, object] = {}
        existing_created_at = ""
        existing_finished_at = ""
        existing_source_commit = ""
        existing_source_ref = ""
        if existing_row is not None:
            try:
                payload = json.loads(str(existing_row["summary_json"] or "{}"))
                if isinstance(payload, dict):
                    existing_summary = dict(payload)
            except Exception:
                existing_summary = {}
            existing_created_at = str(existing_row["created_at"] or "").strip()
            existing_finished_at = str(existing_row["finished_at"] or "").strip()
            existing_source_commit = str(existing_row["source_commit"] or "").strip()
            existing_source_ref = str(existing_row["source_ref"] or "").strip()
        mode_token = "pass-fail"
        if isinstance(summary_extra, dict):
            mode_token = str(summary_extra.get("mode") or "").strip() or mode_token
        if isinstance(existing_summary, dict) and existing_summary:
            mode_token = str(existing_summary.get("mode") or mode_token).strip() or mode_token
        runs_map: dict[str, object] = {}
        existing_runs_obj = existing_summary.get("runs") if isinstance(existing_summary, dict) else None
        if isinstance(existing_runs_obj, dict):
            runs_map = {str(k): dict(v) for k, v in existing_runs_obj.items() if isinstance(v, dict)}
        runs_order: list[str] = []
        existing_order_obj = existing_summary.get("runs_order") if isinstance(existing_summary, dict) else None
        if isinstance(existing_order_obj, list):
            runs_order = [str(item or "").strip() for item in existing_order_obj if str(item or "").strip()]
        source_paths: list[str] = []
        existing_paths_obj = existing_summary.get("source_paths") if isinstance(existing_summary, dict) else None
        if isinstance(existing_paths_obj, list):
            source_paths = [str(item or "").strip() for item in existing_paths_obj if str(item or "").strip()]
        source_commit = existing_source_commit or str(existing_summary.get("source_commit") or "").strip()
        source_ref = existing_source_ref or str(existing_summary.get("source_ref") or "").strip()
        for item in runs:
            run_id = str(item.get("id") or "").strip()
            if not run_id:
                continue
            run_root = config.fs_manager.prepare_verification_run_root(verification_id, run_id).resolve()
            run_root.mkdir(parents=True, exist_ok=True)
            summary_obj = dict(item.get("summary") or {})
            source_label = str(item.get("source_label") or summary_obj.get("source") or run_id).strip() or run_id
            expected_behavior = str(item.get("expected_behavior") or "unknown").strip() or "unknown"
            source_path = str(summary_obj.get("source") or "").strip()
            if source_path and source_path not in source_paths:
                source_paths.append(source_path)
            runs_map[run_id] = {
                "key": run_id,
                "status": str(item.get("status") or status).strip().lower() or "running",
                "source_label": source_label,
                "expected_behavior": expected_behavior,
                "artifact_path": str(item.get("artifact_path") or run_root),
                "task_kind": str(item.get("task_kind") or "").strip(),
                "summary": summary_obj,
            }
            if run_id not in runs_order:
                runs_order.append(run_id)
        summary_extra_obj = dict(summary_extra or {})
        safe_build_id = (
            str(summary_extra_obj.get("artifact_verification_id") or "").strip()
            or str(existing_summary.get("artifact_verification_id") or "").strip()
            or str(build_id or "").strip()
        )
        summary = {
            "kind": str(kind or existing_summary.get("kind") or Kind.VERIFICATION).strip() or Kind.VERIFICATION,
            "mode": mode_token,
            "status": str(status or existing_summary.get("status") or "").strip().lower() or "running",
            "verification_source": "verification.start" if kind == Kind.VERIFICATION else "run.execute",
            "error": str(existing_summary.get("error") or "").strip(),
            "updated_at": created_at,
            "finished_at": finished_at,
            "artifact_root": str(verification_root),
            "source_paths": source_paths,
            "artifact_verification_id": safe_build_id,
            "source_commit": source_commit,
            "source_ref": source_ref,
            "runs_order": runs_order,
            "runs": runs_map,
            "tests": list(existing_summary.get("tests") or []),
            "lifecycle": dict(existing_summary.get("lifecycle") or {"steps": []}),
        }
        if summary_extra_obj:
            if str(summary_extra_obj.get("source_commit") or "").strip():
                source_commit = str(summary_extra_obj.get("source_commit") or "").strip()
            if str(summary_extra_obj.get("source_ref") or "").strip():
                source_ref = str(summary_extra_obj.get("source_ref") or "").strip()
            summary.update(summary_extra_obj)
        final_created_at = existing_created_at or created_at
        final_finished_at = finished_at or existing_finished_at
        if existing_row is None:
            db_execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    problem_id,
                    workspace_id,
                    source_commit,
                    source_ref,
                    kind,
                    status,
                    json.dumps(summary),
                    str(verification_root),
                    final_created_at,
                    final_finished_at or None,
                ],
            )
        else:
            db_execute(
                """
                UPDATE verifications
                SET problem_id=?, workspace_id=?, source_commit=?, source_ref=?, kind=?, status=?, summary_json=?, artifact_path=?, created_at=?, finished_at=?
                WHERE id=?
                """,
                [
                    problem_id,
                    workspace_id,
                    source_commit,
                    source_ref,
                    kind,
                    status,
                    json.dumps(summary),
                    str(verification_root),
                    final_created_at,
                    final_finished_at or None,
                    verification_id,
                ],
            )

    def _insert_stage_verification(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int,
        kind: str = Kind.VERIFICATION,
        source_commit: str = "",
        source_ref: str = "",
        status: str = "ok",
        summary: dict[str, object] | None = None,
        artifact_path: str | None = None,
        created_at: str = "2026-03-10T00:00:00Z",
        finished_at: str | None = "2026-03-10T00:00:01Z",
    ) -> None:
        summary_obj: dict[str, object]
        if isinstance(summary, str):
            try:
                parsed = json.loads(summary)
                summary_obj = dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                summary_obj = {}
        else:
            summary_obj = dict(summary or {})
        root = (
            Path(str(artifact_path)).resolve()
            if artifact_path
            else config.fs_manager.prepare_verification_root(verification_id).resolve()
        )
        root.mkdir(parents=True, exist_ok=True)
        db_execute(
            """
            INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                verification_id,
                problem_id,
                workspace_id,
                str(source_commit or "").strip(),
                str(source_ref or "").strip(),
                str(kind or Kind.VERIFICATION).strip() or Kind.VERIFICATION,
                status,
                json.dumps(summary_obj),
                str(root),
                created_at,
                finished_at,
            ],
        )

    def _insert_verification_run_row(
        self,
        *,
        run_id: str,
        problem_id: int,
        workspace_id: int,
        build_id: str,
        mode: str,
        status: str,
        summary: dict[str, object] | None,
        artifact_path: str,
        created_at: str,
        finished_at: str,
        verification_id: str = "",
        kind: str = "",
        verification_summary_extra: dict[str, object] | None = None,
    ) -> str:
        summary_obj = dict(summary or {})
        verification_obj = summary_obj.get("verification")
        verification = verification_obj if isinstance(verification_obj, dict) else {}
        verification_token = (
            str(verification_id or "").strip()
            or str(verification.get("id") or "").strip()
            or f"ver-{run_id}"
        )
        verification_source = str(verification.get("source") or "").strip().lower()
        inferred_kind = str(kind or "").strip().lower()
        if not inferred_kind:
            inferred_kind = Kind.VERIFICATION
        expected_behavior = str(verification.get("expected_behavior") or "unknown").strip() or "unknown"
        source_label = str(summary_obj.get("source") or run_id).strip() or run_id
        summary_extra: dict[str, object] = {
            "mode": str(mode or summary_obj.get("mode") or "pass-fail").strip() or "pass-fail",
            "artifact_verification_id": str(
                build_id
                or summary_obj.get("artifact_verification_id")
                or summary_obj.get("build_id")
                or ""
            ).strip(),
            "status": str(status or "").strip().lower() or "running",
            "error": str(summary_obj.get("error") or "").strip(),
        }
        if verification_source:
            summary_extra["source"] = verification_source
            summary_extra["verification_source"] = verification_source
        if isinstance(verification_summary_extra, dict):
            summary_extra.update(verification_summary_extra)
        self._insert_verification_row(
            verification_id=verification_token,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=inferred_kind,
            status=str(summary_extra.get("status") or status or "").strip().lower() or "running",
            created_at=created_at,
            finished_at=finished_at,
            runs=[
                {
                    "id": run_id,
                    "status": status,
                    "artifact_path": artifact_path,
                    "source_label": source_label,
                    "expected_behavior": expected_behavior,
                    "summary": summary_obj,
                }
            ],
            summary_extra=summary_extra,
        )
        return verification_token

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

        add_manual = add_manual_call(
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

        add_gen = add_gen_call(
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

        edit_gen = edit_spec_call(
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

        reindex = reindex_spec_call(
            problem="alice/sample",
            user="alice",
            test_id="002",
            target_index="1",
        )
        self.assertEqual(reindex.status_code, 303)
        reindex_loc = str(reindex.headers.get("location", ""))
        self.assertIn("focus=1", reindex_loc)

        delete_second = delete_spec_call(
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

        page = ui_tests_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests/spec.json", html)
        self.assertIn("tests/generator/002.in", html)
        self.assertIn("gen 99", html)
        self.assertIn('class="linkish danger-link" data-submit-form="1">Delete</a>', html)

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
            add_manual_call(problem="alice/sample", user="alice", test_id="001", manual_input="7\n").status_code,
            303,
        )
        self.assertEqual(
            add_gen_call(problem="alice/sample", user="alice", test_id="002", command="gen 10 1").status_code,
            303,
        )
        self.assertEqual(
            add_gen_call(problem="alice/sample", user="alice", test_id="003", command="gen 20 2").status_code,
            303,
        )

        updated = save_gen_script_call(
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

        cleared = save_gen_script_call(problem="alice/sample", user="alice", gen_script_text="")
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

        add_manual = add_manual_call(
            problem="alice/sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        huge_manual = ("A" * 200000) + "\n"
        (manual_dir / "001.in").write_text(huge_manual, encoding="utf-8")

        page = ui_tests_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
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

        add_manual = add_manual_call(
            problem="alice/sample",
            user="alice",
            test_id="001",
            manual_input="seed\n",
        )
        self.assertEqual(add_manual.status_code, 303)

        upload_payload = _FakeUpload(b"7 8 9  \r\n10 11\t \r\n")
        uploaded = asyncio.run(
            upload_payload_call(
                problem="alice/sample",
                user="alice",
                index="1",
                payload_upload=upload_payload,
            )
        )
        self.assertEqual(uploaded.status_code, 303)
        self.assertIn("/problems/alice/sample/alice/tests", uploaded.headers.get("location", ""))
        self.assertEqual((manual_dir / "001.in").read_text(encoding="utf-8"), "7 8 9\n10 11\n")

        downloaded = download_payload_call(problem="alice/sample", user="alice", index="1")
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
            add_manual_upload_call(
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
        add_manual_call(problem="alice/sample", user="alice", test_id="001", manual_input="1\n")
        page = ui_tests_page(_request("/problems/alice/sample/alice/tests"), "alice/sample", "alice")
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
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        db_execute("DELETE FROM verifications WHERE workspace_id=?", [workspace_id])
        artifact_verification_id = self.random_id("ver-run-execute-artifact")
        artifact_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / artifact_verification_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=artifact_verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(artifact_root),
            created_at="2026-03-14T00:00:00Z",
            finished_at="2026-03-14T00:00:01Z",
        )
        def _run_execute_inline(problem: str, user: str, **kwargs):
            workspace_context_job._run_execute_batch_worker(
                problem,
                user,
                **kwargs,
            )
            return True

        with patch("app.impl.workspace.context_job._ensure_implicit_verification", return_value=(artifact_verification_id, False)):
            with patch.object(config.judgehost_task_service, "run_submission", side_effect=RuntimeError("judge unavailable for run execute test")):
                with patch("app.impl.run_export.run.start_run_execute_batch", side_effect=_run_execute_inline):
                    resp = run_execute(
                        problem="alice/sample",
                        user="alice",
                        artifact_verification_id="",
                        solution_paths=["solutions/accepted.cpp"],
                        submission_upload=None,
                    )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/run/details?verification_id=", loc)
        run_messages = _flash_messages_from_response(resp)
        self.assertTrue(run_messages)
        self.assertIn("verification running", run_messages[0])
        query = parse_qs(urlparse(loc).query)
        verification_id = (query.get("verification_id") or [""])[0]
        self.assertTrue(verification_id)
        verification_row = _wait_for_row(
            "SELECT id,status FROM verifications WHERE workspace_id=? AND id=? LIMIT 1",
            [workspace_id, verification_id],
            timeout_sec=10.0,
        )
        self.assertIsNotNone(verification_row)
        self.assertIn(str(verification_row["status"] or ""), {"running", "ok", "failed"})
        deadline = time.monotonic() + 12.0
        resolved_artifact_verification_id = ""
        while time.monotonic() < deadline:
            row = db_fetch_one(
                "SELECT summary_json FROM verifications WHERE workspace_id=? AND id=? LIMIT 1",
                [workspace_id, verification_id],
            )
            summary = json.loads(str(row["summary_json"] or "{}")) if row is not None else {}
            resolved_artifact_verification_id = str(summary.get("artifact_verification_id") or "").strip()
            if resolved_artifact_verification_id and resolved_artifact_verification_id != str(config.constants.RUN_PLACEHOLDER_VERIFICATION_ID):
                break
            time.sleep(0.05)
        self.assertEqual(resolved_artifact_verification_id, artifact_verification_id)
        artifact_row = db_fetch_one("SELECT id FROM verifications WHERE id=? LIMIT 1", [resolved_artifact_verification_id])
        self.assertIsNotNone(artifact_row)

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
            artifact_verification_id="",
            solution_paths=["solutions/accepted.cpp"],
            submission_upload=None,
        )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location", "")
        query = parse_qs(urlparse(loc).query)
        verification_id = (query.get("verification_id") or [""])[0]
        self.assertTrue(verification_id)
        row = _wait_for_row(
            "SELECT summary_json FROM verifications WHERE workspace_id=? AND id=? LIMIT 1",
            [int(workspace_service.workspace_context("alice/sample", "alice", include_recent=False)["workspace"]["id"]), verification_id],
            timeout_sec=8.0,
        )
        self.assertIsNotNone(row)
        summary = json.loads(str(row["summary_json"] or "{}"))
        self.assertEqual(str(summary.get("mode") or ""), "multi-pass")

    def test_run_execute_records_verification_audit_before_queue_start(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        observed = {"checked": False}

        def _fake_start_batch(*args, **kwargs) -> bool:
            verification_id = str(kwargs.get("verification_id") or "")
            verification_run_ids = [str(item or "") for item in (kwargs.get("verification_run_ids") or []) if str(item or "")]
            audit_row = db_fetch_one(
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
            self.assertEqual(str(details.get("verification_id") or ""), verification_id)
            self.assertEqual([str(item or "") for item in (details.get("run_ids") or [])], verification_run_ids)
            self.assertEqual(int(details.get("run_count") or 0), len(verification_run_ids))
            observed["checked"] = True
            return True

        with patch("app.impl.run_export.run.start_run_execute_batch", side_effect=_fake_start_batch):
            resp = run_execute(
                problem="alice/sample",
                user="alice",
                artifact_verification_id="",
                solution_paths=["solutions/accepted.cpp", "solutions/wa.cpp"],
                submission_upload=None,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertTrue(observed["checked"])
        loc = resp.headers.get("location", "")
        self.assertIn("/problems/alice/sample/alice/run/details?verification_id=", loc)
        verification_id = (parse_qs(urlparse(loc).query).get("verification_id") or [""])[0]
        self.assertTrue(verification_id)
        mapped_row = db_fetch_one(
            """
            SELECT details_json
            FROM audit_log
            WHERE problem_id=? AND actor_user_id=? AND action='run.execute'
              AND details_json LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, actor_user_id, f"%{verification_id}%"],
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
                artifact_verification_id="",
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
        accepted_path = ws / "solutions" / "accepted.cpp"
        if accepted_path.exists():
            accepted_path.unlink()
        (ws / "solutions" / "foo.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "bar.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        cfg_path = ws / "config" / "build.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.pop("accepted_solution_source", None)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        start_resp = verification_start(problem=problem, user="alice", page="statement")
        self.assertEqual(start_resp.status_code, 303)
        messages = _flash_messages_from_response(start_resp)
        self.assertTrue(messages)
        self.assertIn("verification failed: main correct solution is required", messages[0])

        row = db_fetch_one(
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

    def test_verification_worker_reuses_buildsolve_for_accepted_solution_without_second_run(self) -> None:
        problem = f"alice/verify-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        build_id = self.random_id("b-verif-buildsolve-reuse")
        verification_id = f"inv-verif-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-verif-accepted-reuse-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-verif-wa-reuse-{uuid.uuid4().hex[:8]}"
        buildsolve_run_id = f"r-buildsolve-reuse-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        buildsolve_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / buildsolve_run_id
        buildsolve_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=workspace_head,
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-11T00:00:00Z",
            finished_at="2026-03-11T00:00:01Z",
        )
        buildsolve_run_summary = {
            "mode": "pass-fail",
            "build_id": build_id,
            "source": "solutions/accepted.cpp",
            "artifact_path": str(buildsolve_root),
            "verification_source": "verification.solve-main",
            "status": "ok",
            "tests": [
                {
                    "test": "001.in",
                    "verdict": "OK",
                    "time_ms": 15,
                    "time_user_ms": 7,
                    "time_wall_ms": 15,
                    "memory_kb": 2048,
                    "feedback_files": ["cache://case/test-key/test-signature/program.err"],
                    "passes": [
                        {
                            "pass": 1,
                            "verdict": "OK",
                            "time_ms": 15,
                            "time_user_ms": 7,
                            "time_wall_ms": 15,
                            "memory_kb": 2048,
                            "output_ref": "cache://case/test-key/test-signature/program.out",
                        }
                    ],
                }
            ],
            "tests_total": 2,
            "error": "",
        }
        db_execute(
            "UPDATE verifications SET summary_json=? WHERE id=?",
            [
                json.dumps(
                    {
                        "stage_results": {
                            "generate_input": {
                                "verification_source": "verification.generate-input",
                                "status": "ok",
                                "tests": [
                                    {"test": "001.in", "verdict": "OK"},
                                    {"test": "002.in", "verdict": "OK"},
                                ],
                            },
                            "solve_main": buildsolve_run_summary,
                        }
                    }
                ),
                build_id,
            ],
        )

        submitted_paths: list[str] = []
        submitted_tests: list[list[str]] = []
        queued_snapshot: dict[str, object] = {}

        def _fake_run_submission(**kwargs):
            run_id = str(kwargs.get("run_id") or "")
            source_path = str(kwargs.get("submission_path") or "")
            submitted_paths.append(source_path)
            submitted_tests.append(list(kwargs.get("selected_tests") or []))
            snapshot_row = db_fetch_one("SELECT summary_json FROM verifications WHERE id=?", [verification_id])
            snapshot_summary = json.loads(str(snapshot_row["summary_json"] or "{}")) if snapshot_row is not None else {}
            queued_snapshot["summary"] = snapshot_summary
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            summary = {
                "mode": "pass-fail",
                "build_id": build_id,
                "source": source_path,
                "verification_source": "verification.start",
                "tests": [{"test": "001.in", "verdict": "WA", "passes": [{"pass": 1, "verdict": "WA"}]}],
                "error": "",
            }
            self._insert_verification_row(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                build_id=build_id,
                kind=Kind.VERIFICATION,
                status="ok",
                created_at="2026-03-11T00:00:04Z",
                finished_at="2026-03-11T00:00:05Z",
                runs=[
                    {
                        "id": run_id,
                        "status": "ok",
                        "source_label": source_path,
                        "expected_behavior": "wrong_answer",
                        "artifact_path": str(run_root),
                        "summary": summary,
                    }
                ],
                summary_extra={
                    "mode": "pass-fail",
                    "build_id": build_id,
                    "verification_source": "verification.start",
                },
            )
            return run_id

        targets = [
            {"path": "solutions/accepted.cpp", "expected_behavior": "accepted", "run_id": accepted_run_id},
            {"path": "solutions/wa.cpp", "expected_behavior": "wrong_answer", "run_id": wa_run_id},
        ]
        with patch("app.impl.workspace.context_job._ensure_implicit_verification", return_value=(build_id, False)):
            with patch.object(config.judgehost_task_service, "run_submission", side_effect=_fake_run_submission):
                workspace_context_job._run_verification_start_worker(
                    problem,
                    "alice",
                    actor_user_id=actor_user_id,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    workspace_head=workspace_head,
                    workspace_dirty=workspace_dirty,
                    targets=targets,
                    verification_id=verification_id,
                )

        self.assertEqual(submitted_paths, ["solutions/wa.cpp"])
        self.assertEqual(submitted_tests, [["001.in", "002.in"]])
        queued_summary = queued_snapshot.get("summary")
        self.assertIsInstance(queued_summary, dict)
        queued_runs = queued_summary.get("runs") if isinstance(queued_summary, dict) else {}
        self.assertIsInstance(queued_runs, dict)
        self.assertEqual(str(((queued_runs.get(accepted_run_id) or {}).get("status") or "")), "ok")
        self.assertEqual(str(((queued_runs.get(wa_run_id) or {}).get("status") or "")), "queued")
        verification_row = db_fetch_one("SELECT summary_json FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        verification_summary = json.loads(str(verification_row["summary_json"] or "{}"))
        runs = verification_summary.get("runs") if isinstance(verification_summary, dict) else {}
        self.assertIsInstance(runs, dict)
        accepted_runs = [
            item
            for item in runs.values()
            if isinstance(item, dict)
            and isinstance(item.get("summary"), dict)
            and str(item["summary"].get("source") or "") == "solutions/accepted.cpp"
        ]
        self.assertEqual(len(accepted_runs), 1)
        accepted_member = runs.get(accepted_run_id)
        self.assertIsInstance(accepted_member, dict)
        self.assertEqual(str(accepted_member.get("status") or ""), "ok")
        accepted_summary = accepted_member.get("summary") if isinstance(accepted_member, dict) else {}
        self.assertIsInstance(accepted_summary, dict)
        self.assertEqual(str(accepted_summary.get("source") or ""), "solutions/accepted.cpp")
        self.assertEqual(str(accepted_summary.get("verification_source") or ""), "verification.solve-main")
        accepted_tests = accepted_summary.get("tests") if isinstance(accepted_summary, dict) else []
        self.assertIsInstance(accepted_tests, list)
        first_test = accepted_tests[0] if accepted_tests else {}
        self.assertEqual(int((first_test or {}).get("time_user_ms") or 0), 7)
        self.assertEqual(int((first_test or {}).get("time_wall_ms") or 0), 15)
        self.assertEqual(int((first_test or {}).get("memory_kb") or 0), 2048)
        first_passes = (first_test or {}).get("passes") if isinstance(first_test, dict) else []
        first_pass = first_passes[0] if isinstance(first_passes, list) and first_passes else {}
        self.assertEqual(str((first_pass or {}).get("output_ref") or ""), "cache://case/test-key/test-signature/program.out")
        self.assertFalse(
            any(
                str(((item.get("summary") or {}) if isinstance(item, dict) else {}).get("verification_source") or "")
                == "verification.start"
                for item in accepted_runs
            )
        )

    def test_verification_worker_persists_placeholder_runs_before_build_resolution(self) -> None:
        problem = f"alice/verify-placeholder-runs-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        verification_id = f"inv-verif-placeholder-runs-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-verif-placeholder-accepted-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-verif-placeholder-wa-{uuid.uuid4().hex[:8]}"
        captured_summary: dict[str, object] = {}

        def _capture_then_fail(*args, **kwargs):
            row = db_fetch_one("SELECT summary_json FROM verifications WHERE id=?", [verification_id])
            self.assertIsNotNone(row)
            payload = json.loads(str(row["summary_json"] or "{}"))
            self.assertIsInstance(payload, dict)
            captured_summary.update(payload)
            raise RuntimeError("stop after placeholder persistence")

        targets = [
            {"path": "solutions/accepted.cpp", "expected_behavior": "accepted", "run_id": accepted_run_id},
            {"path": "solutions/wa.cpp", "expected_behavior": "wrong_answer", "run_id": wa_run_id},
        ]
        with patch("app.impl.workspace.context_job._ensure_implicit_verification", side_effect=_capture_then_fail):
            workspace_context_job._run_verification_start_worker(
                problem,
                "alice",
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
                targets=targets,
                verification_id=verification_id,
            )

        self.assertEqual(captured_summary.get("status"), "running")
        self.assertEqual(captured_summary.get("runs_order"), [accepted_run_id, wa_run_id])
        runs = captured_summary.get("runs")
        self.assertIsInstance(runs, dict)
        self.assertEqual(str(((runs.get(accepted_run_id) or {}).get("status") or "")), "queued")
        self.assertEqual(str(((runs.get(wa_run_id) or {}).get("status") or "")), "queued")
        accepted_summary = (runs.get(accepted_run_id) or {}).get("summary") if isinstance(runs, dict) else {}
        wa_summary = (runs.get(wa_run_id) or {}).get("summary") if isinstance(runs, dict) else {}
        self.assertEqual(str((accepted_summary or {}).get("source") or ""), "solutions/accepted.cpp")
        self.assertEqual(str((wa_summary or {}).get("source") or ""), "solutions/wa.cpp")

    def test_verification_worker_retry_for_accepted_solution_keeps_selected_tests(self) -> None:
        problem = f"alice/verify-retry-tests-{uuid.uuid4().hex[:8]}"
        self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        verification_id = f"ver-retry-tests-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-ver-retry-tests")

        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=workspace_head,
            source_ref="main",
            status="ok",
            summary={
                "stage_results": {
                    "generate_input": {
                        "verification_source": "verification.generate-input",
                        "status": "ok",
                        "tests": [
                            {"test": "001.in", "verdict": "OK"},
                            {"test": "002.in", "verdict": "OK"},
                        ],
                    }
                }
            },
            artifact_path=str(build_root),
            created_at="2026-03-14T00:00:00Z",
            finished_at="2026-03-14T00:00:01Z",
        )

        submitted_calls: list[dict[str, object]] = []
        accepted_run_ids = [f"r-retry-first-{uuid.uuid4().hex[:8]}", f"r-retry-second-{uuid.uuid4().hex[:8]}"]

        def _fake_run_submission(**kwargs):
            source_path = str(kwargs.get("submission_path") or "")
            submitted_calls.append(
                {
                    "source_path": source_path,
                    "selected_tests": list(kwargs.get("selected_tests") or []),
                    "expected_behavior": str(kwargs.get("expected_behavior") or ""),
                }
            )
            run_id = str(kwargs.get("run_id") or "")
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            call_index = len(submitted_calls)
            verdict = "WA" if call_index == 1 else "OK"
            summary = {
                "mode": "pass-fail",
                "build_id": build_id,
                "source": source_path,
                "verification_source": "verification.start",
                "tests": [
                    {
                        "test": "001.in",
                        "verdict": verdict,
                        "passes": [{"pass": 1, "verdict": verdict}],
                    }
                ],
                "error": "",
            }
            self._insert_verification_row(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                build_id=build_id,
                kind=Kind.VERIFICATION,
                status="ok",
                created_at="2026-03-14T00:00:02Z",
                finished_at="2026-03-14T00:00:03Z",
                runs=[
                    {
                        "id": run_id,
                        "status": "ok",
                        "source_label": source_path,
                        "expected_behavior": "accepted",
                        "artifact_path": str(run_root),
                        "summary": summary,
                    }
                ],
                summary_extra={
                    "mode": "pass-fail",
                    "build_id": build_id,
                    "verification_source": "verification.start",
                },
            )
            return run_id

        targets = [
            {"path": "solutions/accepted.cpp", "expected_behavior": "accepted", "run_id": accepted_run_ids[0]},
        ]
        with patch("app.impl.workspace.context_job._ensure_implicit_verification", return_value=(build_id, False)):
            with patch.object(config.judgehost_task_service, "run_submission", side_effect=_fake_run_submission):
                workspace_context_job._run_verification_start_worker(
                    problem,
                    "alice",
                    actor_user_id=actor_user_id,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    workspace_head=workspace_head,
                    workspace_dirty=workspace_dirty,
                    targets=targets,
                    verification_id=verification_id,
                )

        self.assertEqual(len(submitted_calls), 2)
        self.assertEqual(
            [call.get("selected_tests") for call in submitted_calls],
            [["001.in", "002.in"], ["001.in", "002.in"]],
        )
        self.assertEqual(
            [str(call.get("source_path") or "") for call in submitted_calls],
            ["solutions/accepted.cpp", "solutions/accepted.cpp"],
        )

    def test_verification_worker_keeps_running_when_non_main_run_has_only_running_placeholder(self) -> None:
        problem = f"alice/verify-running-placeholder-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        build_id = self.random_id("b-verif-running-placeholder")
        verification_id = f"ver-running-placeholder-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-verif-accepted-running-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-verif-wa-running-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / problem / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit=workspace_head,
            source_ref="main",
            status="ok",
            summary={
                "stage_results": {
                    "generate_input": {
                        "verification_source": "verification.generate-input",
                        "status": "ok",
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                    },
                    "solve_main": {
                        "verification_source": "verification.solve-main",
                        "status": "ok",
                        "source": "solutions/accepted.cpp",
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                    },
                }
            },
            artifact_path=str(build_root),
            created_at="2026-03-14T00:00:00Z",
            finished_at="2026-03-14T00:00:01Z",
        )

        def _fake_run_submission(**kwargs):
            run_id = str(kwargs.get("run_id") or "")
            source_path = str(kwargs.get("submission_path") or "")
            run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            self._insert_verification_row(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                build_id=build_id,
                kind=Kind.VERIFICATION,
                status="running",
                created_at="2026-03-14T00:00:02Z",
                finished_at="",
                runs=[
                    {
                        "id": run_id,
                        "status": "running",
                        "source_label": source_path,
                        "expected_behavior": "wrong_answer",
                        "artifact_path": str(run_root),
                        "summary": {
                            "mode": "pass-fail",
                            "source": source_path,
                            "verification_source": "verification.start",
                            "tests": [],
                            "error": "",
                        },
                    }
                ],
                summary_extra={
                    "mode": "pass-fail",
                    "verification_source": "verification.start",
                },
            )
            return run_id

        targets = [
            {"path": "solutions/accepted.cpp", "expected_behavior": "accepted", "run_id": accepted_run_id},
            {"path": "solutions/wa.cpp", "expected_behavior": "wrong_answer", "run_id": wa_run_id},
        ]
        with patch("app.impl.workspace.context_job._ensure_implicit_verification", return_value=(build_id, False)):
            with patch.object(config.judgehost_task_service, "run_submission", side_effect=_fake_run_submission):
                workspace_context_job._run_verification_start_worker(
                    problem,
                    "alice",
                    actor_user_id=actor_user_id,
                    problem_id=problem_id,
                    workspace_id=workspace_id,
                    workspace_head=workspace_head,
                    workspace_dirty=workspace_dirty,
                    targets=targets,
                    verification_id=verification_id,
                )

        verification_row = db_fetch_one("SELECT status,summary_json,finished_at FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or ""), "running")
        self.assertEqual(str(verification_row["finished_at"] or ""), "")
        verification_summary = json.loads(str(verification_row["summary_json"] or "{}"))
        self.assertEqual(str(verification_summary.get("status") or ""), "running")
        self.assertEqual(str(verification_summary.get("error") or ""), "")
        runs = verification_summary.get("runs") if isinstance(verification_summary, dict) else {}
        self.assertIsInstance(runs, dict)
        self.assertEqual(str(((runs.get(accepted_run_id) or {}).get("status") or "")), "ok")
        self.assertEqual(str(((runs.get(wa_run_id) or {}).get("status") or "")), "running")

    def test_verification_sidebar_marks_stale_when_gen_chk_sol_tests_change(self) -> None:
        problem = f"alice/verify-stale-{uuid.uuid4().hex[:8]}"
        ws = self._prepare_verification_workspace(problem)
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        signature = workspace_impl._verification_sources_signature(ws)
        signature_details = workspace_impl._verification_sources_signature_details(ws)

        self._insert_verification_row(
            verification_id=f"ver-stale-{uuid.uuid4().hex[:8]}",
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-stale"),
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
            runs=[],
            summary_extra={
                "status": "pass",
                "workspace_head": workspace_head,
                "workspace_dirty": workspace_dirty,
                "verification_signature": signature,
                "verification_signature_details": signature_details,
            },
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
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        signature = workspace_impl._verification_sources_signature(ws)
        signature_details = workspace_impl._verification_sources_signature_details(ws)

        self._insert_verification_row(
            verification_id=f"ver-stale-general-{uuid.uuid4().hex[:8]}",
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-stale-general"),
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
            runs=[],
            summary_extra={
                "status": "pass",
                "workspace_head": workspace_head,
                "workspace_dirty": workspace_dirty,
                "verification_signature": signature,
                "verification_signature_details": signature_details,
            },
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

    def test_run_list_rejudge_link_uses_verification_id_and_run_new_resolves_paths(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-rerun-link-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-rerun-link-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-rerun-link-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-rerun-link")
        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK"}],
            "verification": {
                "id": verification_id,
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
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-03-03T00:00:01Z",
            finished_at="2026-03-03T00:00:04Z",
            runs=[
                {
                    "id": run_ok,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": dict(summary_ok),
                },
                {
                    "id": run_wa,
                    "status": "ok",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": dict(summary_wa),
                },
            ],
        )
        list_page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(list_page.status_code, 200)
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertIn(f"/run/new?rerun_verification_id={verification_id}&force_recompile=1", list_html)
        self.assertNotIn("/run/new?solution_paths=", list_html)

        new_page = run_new_page(
            _request("/problems/alice/sample/alice/run/new", f"rerun_verification_id={verification_id}"),
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

        add_manual_1 = add_manual_call(problem=problem, user="alice", test_id="001", manual_input="1\n")
        add_manual_2 = add_manual_call(problem=problem, user="alice", test_id="002", manual_input="2\n")
        self.assertEqual(add_manual_1.status_code, 303)
        self.assertEqual(add_manual_2.status_code, 303)

        page = run_new_page(_request(f"/problems/{problem}/alice/run/new"), problem, "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn('name="test_names" value="001.in" checked', html)
        self.assertIn('name="test_names" value="002.in" checked', html)

    def test_run_page_uses_default_sidebar_without_verification_table(self) -> None:
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
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-oversized-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-oversized-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-oversized-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-inv-oversized")
        oversized_blob = "x" * 70000
        base_summary = {
            "mode": "pass-fail",
            "tests": [{"test": "001.in", "verdict": "OK", "blob": oversized_blob}],
        }

        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.execute",
                json.dumps(
                    {
                        "status": "queued",
                        "verification_id": verification_id,
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
            run_root = config.fs_manager.prepare_verification_run_root(f"ver-{run_id}", run_id).resolve()
            run_root.mkdir(parents=True, exist_ok=True)
            self._insert_verification_run_row(
                run_id=run_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                build_id=build_id,
                mode="pass-fail",
                status=status,
                summary=summary,
                artifact_path=str(run_root),
                created_at=created_at,
                finished_at=finished_at,
            )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        rows_by_id = {str(item.get("id") or ""): item for item in rows}
        verification_ok = f"ver-{run_ok}"
        verification_wa = f"ver-{run_wa}"
        self.assertIn(verification_ok, rows_by_id)
        self.assertIn(verification_wa, rows_by_id)
        self.assertEqual(int(rows_by_id[verification_ok].get("run_count") or 0), 1)
        self.assertEqual(int(rows_by_id[verification_wa].get("run_count") or 0), 1)

    def test_run_list_does_not_backfill_missing_run_ids_from_audit(self) -> None:
        problem = f"alice/inv-verify-map-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verify-map-{uuid.uuid4().hex[:8]}"
        run_a = f"r-verify-map-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-verify-map-b-{uuid.uuid4().hex[:8]}"

        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "verification_id": verification_id,
                        "run_id": run_a,
                        "run_ids": [run_a, run_b],
                        "run_count": 2,
                    }
                ),
                "2026-02-23T00:00:00Z",
            ],
        )

        summary = {"mode": "pass-fail", "source": "solutions/accepted.cpp", "tests": []}
        run_root = config.fs_manager.prepare_verification_run_root(verification_id, run_a).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        self._insert_verification_run_row(
            run_id=run_a,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-verify-map"),
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:01Z",
            finished_at="",
            verification_id=verification_id,
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        rows_by_id = {str(item.get("id") or ""): item for item in rows}
        self.assertIn(verification_id, rows_by_id)
        self.assertNotIn(run_b, list(rows_by_id))
        self.assertEqual(int(rows_by_id[verification_id].get("run_count") or 0), 1)
        self.assertEqual(list(rows_by_id[verification_id].get("run_ids") or []), [run_a])

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
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-audit-pending-{uuid.uuid4().hex[:8]}"
        run_a = f"r-audit-pending-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-audit-pending-b-{uuid.uuid4().hex[:8]}"

        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "mode": "pass-fail",
                        "verification_id": verification_id,
                        "run_id": run_a,
                        "run_ids": [run_a, run_b],
                        "run_count": 2,
                        "submission_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                    }
                ),
                "2026-03-02T00:00:00Z",
            ],
        )

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-audit-pending"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-02T00:00:00Z",
            finished_at="",
            runs=[
                {"id": run_a, "status": "running", "source_label": "solutions/accepted.cpp", "summary": {"source": "solutions/accepted.cpp", "mode": "pass-fail", "tests": []}},
                {"id": run_b, "status": "running", "source_label": "solutions/wa.cpp", "summary": {"source": "solutions/wa.cpp", "mode": "pass-fail", "tests": []}},
            ],
            summary_extra={
                "mode": "pass-fail",
                "verification_source": "verification.start",
                "run_ids": [run_a, run_b],
                "run_count": 2,
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
            },
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), verification_id)
        self.assertEqual(str(rows[0].get("status") or ""), "running")
        self.assertTrue(bool(rows[0].get("has_running")))
        self.assertEqual(int(rows[0].get("run_count") or 0), 2)
        self.assertIn("solutions/accepted.cpp", str(rows[0].get("source_display") or ""))
        self.assertIn("solutions/wa.cpp", str(rows[0].get("source_display") or ""))
        self.assertEqual(str(rows[0].get("tests_label") or ""), "tests: in progress")

    def test_run_list_shows_running_verification_from_submission_paths_before_runs_exist(self) -> None:
        problem = f"alice/verification-submission-paths-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"ver-submission-paths-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-submission-paths"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-13T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "verification_source": "verification.start",
                "submission_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
            },
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), verification_id)
        self.assertEqual(str(rows[0].get("status") or ""), "running")
        self.assertTrue(bool(rows[0].get("has_running")))
        self.assertEqual(int(rows[0].get("run_count") or 0), 2)
        self.assertIn("solutions/accepted.cpp", str(rows[0].get("source_display") or ""))
        self.assertIn("solutions/wa.cpp", str(rows[0].get("source_display") or ""))
        self.assertEqual(str(rows[0].get("tests_label") or ""), "tests: in progress")

    def test_run_list_shows_running_verification_from_solution_count_without_paths(self) -> None:
        problem = f"alice/verification-solution-count-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"])
        verification_id = f"ver-solution-count-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-solution-count"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-13T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "verification_source": "verification.start",
                "solution_count": 3,
            },
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), verification_id)
        self.assertEqual(str(rows[0].get("status") or ""), "running")
        self.assertTrue(bool(rows[0].get("has_running")))
        self.assertEqual(int(rows[0].get("run_count") or 0), 3)
        self.assertEqual(str(rows[0].get("tests_label") or ""), "tests: in progress")

    def test_run_list_keeps_top_level_verification_when_summary_source_is_solve_main(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-buildsolve-{uuid.uuid4().hex[:8]}"
        run_id = f"r-buildsolve-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-buildsolve"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-03T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests": [],
                        "error": "",
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "verification_source": "verification.solve-main",
                "source_paths": ["solutions/accepted.cpp"],
            },
        )
        rows = workspace_impl.run_list_rows(problem_id, workspace_id, Path(ctx["workspace"]["path"]), limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].get("id") or ""), verification_id)
        self.assertEqual(str(rows[0].get("status") or ""), "running")
        self.assertTrue(bool(rows[0].get("has_running")))

    def test_run_list_excludes_generate_stage_runs_from_solution_summary(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-list-hidden-{uuid.uuid4().hex[:8]}"
        manual_run_id = f"r-list-hidden-manual-{uuid.uuid4().hex[:8]}"
        generator_run_id = f"r-list-hidden-generator-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-list-hidden-accepted-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-list-hidden-wa-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-list-hidden"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-16T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": manual_run_id,
                    "status": "ok",
                    "source_label": "manual_validate.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "manual_validate.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": generator_run_id,
                    "status": "ok",
                    "source_label": "gen.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "gen.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": accepted_run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 3,
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": wa_run_id,
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests_total": 0,
                        "tests": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "status": "running",
                "verification_source": "verification.start",
                "source_paths": [
                    "manual_validate.cpp",
                    "gen.cpp",
                    "solutions/accepted.cpp",
                    "solutions/wa.cpp",
                ],
            },
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("id") or ""), verification_id)
        self.assertEqual(int(row.get("run_count") or 0), 2)
        self.assertEqual(list(row.get("run_ids") or []), [accepted_run_id, wa_run_id])
        self.assertIn("solutions/accepted.cpp", str(row.get("source_display") or ""))
        self.assertIn("solutions/wa.cpp", str(row.get("source_display") or ""))
        self.assertNotIn("manual_validate.cpp", str(row.get("source_display") or ""))
        self.assertNotIn("gen.cpp", str(row.get("source_display") or ""))
        self.assertEqual(list(row.get("rerun_solution_paths") or []), [])

    def test_run_list_summary_ignores_generate_input_runs_for_count_source_display_and_rerun_paths(self) -> None:
        problem = f"alice/run-list-hidden-generate-{uuid.uuid4().hex[:8]}"
        workspace_service.ensure_problem(problem, f"{problem} title")
        ws = Path(workspace_service.ensure_workspace(problem, "alice"))
        workspace_service.grant_repo_access(problem, "alice", "owner")
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context(problem, "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"ver-list-hidden-generate-{uuid.uuid4().hex[:8]}"
        manual_run_id = f"r-list-hidden-manual-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-list-hidden-gen-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-list-hidden-accepted-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-list-hidden-wa-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-list-hidden-generate"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-15T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": manual_run_id,
                    "status": "running",
                    "source_label": "manual_validate.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "manual_validate.cpp",
                        "verification_source": "verification.generate-input",
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": gen_run_id,
                    "status": "running",
                    "source_label": "generators/gen.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "generators/gen.cpp",
                        "verification_source": "verification.generate-input",
                        "tests": [{"test": "002.in", "verdict": ""}],
                        "error": "",
                    },
                },
                {
                    "id": accepted_run_id,
                    "status": "queued",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests": [],
                        "error": "",
                    },
                },
                {
                    "id": wa_run_id,
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "mode": "pass-fail",
                "verification_source": "verification.start",
            },
        )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=20, actor_user_id=actor_user_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.get("id") or ""), verification_id)
        self.assertEqual(int(row.get("run_count") or 0), 2)
        self.assertEqual(str(row.get("source_display") or ""), "solutions/accepted.cpp, solutions/wa.cpp")
        self.assertEqual(
            list(row.get("rerun_solution_paths") or []),
            ["solutions/accepted.cpp", "solutions/wa.cpp"],
        )
        self.assertNotIn("manual_validate.cpp", str(row.get("source_display") or ""))
        self.assertNotIn("generators/gen.cpp", str(row.get("source_display") or ""))

    def test_run_details_hides_generate_input_runs_as_columns_and_shows_test_stage_cells_in_first_column(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        build_id = self.random_id("b-verif-stage-markers")
        verification_id = f"ver-test-stage-cells-{uuid.uuid4().hex[:8]}"
        accepted_run_id = f"r-stage-accepted-{uuid.uuid4().hex[:8]}"
        wa_run_id = f"r-stage-wa-{uuid.uuid4().hex[:8]}"
        manual_run_id = f"r-stage-manual-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-stage-gen-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-10T00:00:02Z",
            finished_at="",
            runs=[
                {
                    "id": manual_run_id,
                    "status": "running",
                    "source_label": "manual_validate.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "manual_validate.cpp",
                        "verification_source": "verification.generate-input",
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": gen_run_id,
                    "status": "running",
                    "source_label": "generators/gen.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "generators/gen.cpp",
                        "verification_source": "verification.generate-input",
                        "selected_tests_count": 2,
                        "tests": [{"test": "002.in", "verdict": ""}],
                        "error": "",
                    },
                },
                {
                    "id": accepted_run_id,
                    "status": "queued",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests": [],
                        "error": "",
                    },
                },
                {
                    "id": wa_run_id,
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "mode": "pass-fail",
                "verification_source": "verification.start",
                "artifact_verification_id": build_id,
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                "solution_count": 2,
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("accepted.cpp", html)
        self.assertIn("wa.cpp", html)
        self.assertNotIn("manual_validate.cpp", html)
        self.assertNotIn("generators/gen.cpp", html)
        self.assertIn('class="vcell tcell tone-ok"', html)
        self.assertIn('class="vcell tcell tone-running"', html)
        self.assertIn('<span class="vcode">..</span>', html)
        self.assertIn('<span class="vmeta">generating</span>', html)

    def test_run_list_orders_by_verification_run_time_not_latest_run_time(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])

        old_verification = f"ver-old-{uuid.uuid4().hex[:8]}"
        new_verification = f"ver-new-{uuid.uuid4().hex[:8]}"
        old_run_1 = f"r-old-1-{uuid.uuid4().hex[:8]}"
        old_run_2 = f"r-old-2-{uuid.uuid4().hex[:8]}"
        new_run = f"r-new-{uuid.uuid4().hex[:8]}"

        def _summary(verification_id: str, run_ids: list[str], source: str) -> dict[str, object]:
            return {
                "source": source,
                "verification": {
                    "id": verification_id,
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
            (old_run_1, old_verification, [old_run_1, old_run_2], "solutions/old-1.cpp", "2026-03-03T00:00:00Z"),
            (new_run, new_verification, [new_run], "solutions/new.cpp", "2026-03-03T00:05:00Z"),
            # Later run of the old verification must not move old verification ahead of new verification.
            (old_run_2, old_verification, [old_run_1, old_run_2], "solutions/old-2.cpp", "2026-03-03T00:10:00Z"),
        ]
        for run_id, verification_id, run_ids, source, created_at in run_specs:
            self._insert_verification_run_row(
                run_id=run_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                build_id=self.random_id("b-order"),
                mode="pass-fail",
                status="ok",
                summary=_summary(verification_id, run_ids, source),
                artifact_path=str(config.fs_manager.prepare_verification_run_root(verification_id, run_id).resolve()),
                created_at=created_at,
                finished_at=created_at,
                verification_id=verification_id,
                kind=Kind.VERIFICATION,
            )

        rows = workspace_impl.run_list_rows(problem_id, workspace_id, ws, limit=10, actor_user_id=int(ctx["user"]["id"]))
        ordered_ids = [str(item.get("id") or "") for item in rows]
        self.assertIn(old_verification, ordered_ids)
        self.assertIn(new_verification, ordered_ids)
        self.assertLess(ordered_ids.index(new_verification), ordered_ids.index(old_verification))
        old_row = next((item for item in rows if str(item.get("id") or "") == old_verification), {})
        self.assertEqual(str(old_row.get("created_at") or ""), "2026-03-03T00:00:00Z")

    def test_run_details_tracks_verification_scope_across_async_refreshes(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-refresh-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-refresh-ok-{uuid.uuid4().hex[:8]}"
        run_wa = f"r-refresh-wa-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-inv-refresh")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_ok_root = build_root / "logs" / f"run-{run_ok}"
        run_wa_root = build_root / "logs" / f"run-{run_wa}"
        run_ok_root.mkdir(parents=True, exist_ok=True)
        run_wa_root.mkdir(parents=True, exist_ok=True)

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                "verification_source": "verification.start",
            },
        )

        first = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(first.status_code, 200)
        first_html = first.body.decode("utf-8", errors="replace")
        self.assertNotIn('class="col-title">accepted.cpp</span>', first_html)
        self.assertNotIn('class="col-title">wa.cpp</span>', first_html)
        self.assertIn("Verification Progress", first_html)
        self.assertNotIn("Auto-refreshing every 2 seconds.", first_html)
        self.assertNotIn("window.location.reload", first_html)
        self.assertIn("Per-test details will appear once verification runs report testcase results.", first_html)

        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]}],
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:01Z",
            finished_at="",
            runs=[
                {
                    "id": run_ok,
                    "status": "ok",
                    "artifact_path": str(run_ok_root),
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": summary_ok,
                }
            ],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                "verification_source": "verification.start",
            },
        )

        second = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(second.status_code, 200)
        second_html = second.body.decode("utf-8", errors="replace")
        self.assertIn('class="col-title">accepted.cpp</span>', second_html)
        self.assertNotIn('class="col-title">wa.cpp</span>', second_html)
        self.assertNotIn("Expected match:", second_html)

        summary_wa = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": [{"test": "001.in", "verdict": "WA", "passes": [{"pass": 1, "verdict": "WA"}]}],
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-02-23T00:00:03Z",
            finished_at="2026-02-23T00:00:04Z",
            runs=[
                {
                    "id": run_wa,
                    "status": "ok",
                    "artifact_path": str(run_wa_root),
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": summary_wa,
                }
            ],
            summary_extra={
                "status": "ok",
                "verification_id": verification_id,
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                "verification_source": "verification.start",
            },
        )

        third = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(third.status_code, 200)
        third_html = third.body.decode("utf-8", errors="replace")
        self.assertIn('class="col-title">accepted.cpp</span>', third_html)
        self.assertIn('class="col-title">wa.cpp</span>', third_html)
        self.assertNotIn("Expected match:", third_html)

    def test_rejudge_unavailable_consistent_between_list_and_details_while_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-rejudge-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-rejudge-ok-{uuid.uuid4().hex[:8]}"
        run_pending = f"r-rejudge-pending-{uuid.uuid4().hex[:8]}"
        run_ok_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_ok
        run_pending_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_pending
        run_ok_root.mkdir(parents=True, exist_ok=True)
        run_pending_root.mkdir(parents=True, exist_ok=True)
        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [{"test": "001.in", "verdict": "OK"}],
        }
        summary_pending = {
            "mode": "pass-fail",
            "source": "solutions/wa.cpp",
            "tests": [],
            "tests_total": 1,
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-rejudge"),
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:01Z",
            finished_at="",
            runs=[
                {
                    "id": run_ok,
                    "status": "ok",
                    "artifact_path": str(run_ok_root),
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": summary_ok,
                },
                {
                    "id": run_pending,
                    "status": "running",
                    "artifact_path": str(run_pending_root),
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": summary_pending,
                },
            ],
            summary_extra={
                "status": "running",
                "verification_source": "verification.start",
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
            },
        )

        list_page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        list_html = list_page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Rejudge unavailable:", list_html)
        self.assertNotIn(">Rejudge</a>", list_html)
        self.assertNotIn("/run/new?solution_paths=solutions%2Faccepted.cpp", list_html)

        details_page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        details_html = details_page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Rejudge unavailable:", details_html)
        self.assertNotIn(">Rejudge</button>", details_html)
        self.assertIn("Verification Progress", details_html)

    def test_run_cancel_marks_running_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-cancel-{uuid.uuid4().hex[:8]}"
        run_running = f"r-cancel-running-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_running
        run_root.mkdir(parents=True, exist_ok=True)
        summary_running = {
            "mode": "pass-fail",
            "build_id": self.random_id("b-cancel-run"),
            "source": "solutions/accepted.cpp",
            "tests_total": 5,
            "tests": [],
        }
        build_id = str(summary_running["build_id"])
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
            runs=[
                {
                    "id": run_running,
                    "status": "running",
                    "artifact_path": str(run_root),
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": summary_running,
                }
            ],
            summary_extra={
                "mode": "pass-fail",
                "build_id": build_id,
                "verification_source": "verification.start",
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
            },
        )

        details_before = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        before_html = details_before.body.decode("utf-8", errors="replace")
        self.assertIn(">Cancel</a>", before_html)
        self.assertIn('class="linkish danger-link" data-submit-form="1">Cancel</a>', before_html)
        self.assertIn("Verification Progress", before_html)

        cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        self.assertIn(
            f"/problems/alice/sample/alice/run/details?verification_id={verification_id}",
            str(cancel_resp.headers.get("location", "")),
        )
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("cancel requested", cancel_messages[0])

        verification_row = db_fetch_one("SELECT summary_json,status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        verification_summary = json.loads(str(verification_row["summary_json"] or "{}"))
        runs = verification_summary.get("runs") if isinstance(verification_summary, dict) else {}
        self.assertIsInstance(runs, dict)
        running_member = runs.get(run_running)
        self.assertIsInstance(running_member, dict)
        self.assertEqual(str(running_member.get("status") or "").lower(), "failed")
        running_summary_after = running_member.get("summary") if isinstance(running_member, dict) else {}
        self.assertIsInstance(running_summary_after, dict)
        self.assertTrue(bool(running_summary_after.get("cancelled")))
        self.assertIn("cancelled by user", str(running_summary_after.get("error") or ""))
        self.assertEqual(int(running_summary_after.get("tests_total") or 0), 5)
        self.assertIsInstance(running_summary_after.get("tests"), list)
        self.assertEqual(len(running_summary_after.get("tests") or []), 0)

        details_after = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        after_html = details_after.body.decode("utf-8", errors="replace")
        self.assertIn("Verification status:", after_html)
        self.assertIn("FAILED", after_html)
        self.assertNotIn(">Cancel</a>", after_html)

    def test_run_cancel_ignores_late_leased_case_callback(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-cancel-late-{uuid.uuid4().hex[:8]}"
        run_id = f"r-cancel-late-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-cancel-late")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        (build_root / "tests").mkdir(parents=True, exist_ok=True)
        (build_root / "ans").mkdir(parents=True, exist_ok=True)
        (build_root / "tests" / "001.in").write_text("1\n", encoding="utf-8")
        (build_root / "ans" / "001.ans").write_text("1\n", encoding="utf-8")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-17T00:00:00Z",
            finished_at=None,
        )

        service = config.judgehost_task_service
        old_enabled = service._enabled
        old_token = service._api_token
        old_username = service._api_username
        old_include_build_payload = service._include_build_payload
        self.addCleanup(setattr, service, "_enabled", old_enabled)
        self.addCleanup(setattr, service, "_api_token", old_token)
        self.addCleanup(setattr, service, "_api_username", old_username)
        self.addCleanup(setattr, service, "_include_build_payload", old_include_build_payload)
        service._enabled = True
        service._api_token = "test-token"
        service._api_username = "judgehost"
        service._include_build_payload = True
        service.domjudge_register_host("judgehost-cancel-late")
        service.enqueue_task(
            problem="alice/sample",
            username="alice",
            artifact_verification_id=build_id,
            mode="pass-fail",
            submission_path="solutions/accepted.cpp",
            upload_content=None,
            upload_filename=None,
            run_id=run_id,
            selected_tests=["001.in"],
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="accepted",
            verification_source="run.execute",
        )
        leased = service.domjudge_fetch_work("judgehost-cancel-late", max_batchsize=8)
        self.assertEqual(len(leased), 1)
        case_id = int(leased[0].get("judgetaskid") or 0)
        self.assertGreater(case_id, 0)

        cancel_resp = run_export_impl.run_cancel(problem="alice/sample", user="alice", verification_id=verification_id)
        self.assertEqual(cancel_resp.status_code, 303)
        cancel_messages = _flash_messages_from_response(cancel_resp)
        self.assertTrue(cancel_messages)
        self.assertIn("cancel requested", cancel_messages[0])

        meta_text = "cpu-time: 0.004\nwall-time: 0.004\nmemory-bytes: 4096\n"
        service.domjudge_add_judging_run(
            "judgehost-cancel-late",
            case_id,
            {
                "runresult": "correct",
                "runtime": "0.004",
                "output_run": base64.b64encode(b"1\n").decode("ascii"),
                "output_diff": "",
                "output_error": "",
                "output_system": "",
                "metadata": base64.b64encode(meta_text.encode("utf-8")).decode("ascii"),
                "compare_metadata": "",
            },
        )

        verification_row = db_fetch_one("SELECT status, summary_json FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or "").lower(), "failed")
        verification_summary = json.loads(str(verification_row["summary_json"] or "{}"))
        runs = verification_summary.get("runs") if isinstance(verification_summary, dict) else {}
        self.assertIsInstance(runs, dict)
        run_payload = runs.get(run_id) if isinstance(runs, dict) else None
        self.assertIsInstance(run_payload, dict)
        self.assertEqual(str(run_payload.get("status") or "").lower(), "failed")
        run_summary = run_payload.get("summary") if isinstance(run_payload, dict) else {}
        self.assertIsInstance(run_summary, dict)
        self.assertTrue(bool(run_summary.get("cancelled")))
        self.assertEqual(run_summary.get("tests") or [], [])

    def test_finalize_cancelled_verifications_marks_verification_failed_without_active_runs(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"ver-artifact-cancel-finalize-{uuid.uuid4().hex[:8]}"
        run_id = f"r-cancel-finalize-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={"step": "run"},
            artifact_path=str(build_root),
            created_at="2026-03-05T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary={},
            artifact_path=str(run_root),
            created_at="2026-03-05T00:00:01Z",
            finished_at="2026-03-05T00:00:02Z",
        )

        cancelled = run_export_impl._finalize_cancelled_verifications([build_id], "verification cancelled by user")
        self.assertEqual(cancelled, 1)
        build_row = db_fetch_one("SELECT status,summary_json FROM verifications WHERE id=?", [build_id])
        self.assertIsNotNone(build_row)
        self.assertEqual(str(build_row["status"] or "").strip().lower(), "failed")
        summary = json.loads(str(build_row["summary_json"] or "{}"))
        self.assertTrue(bool(summary.get("cancelled")))
        self.assertIn("cancelled by user", str(summary.get("cancel_reason") or ""))

    def test_finalize_cancelled_verifications_skips_when_other_active_runs_exist(self) -> None:
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = f"ver-artifact-cancel-active-{uuid.uuid4().hex[:8]}"
        run_cancelled_id = f"r-cancel-active-failed-{uuid.uuid4().hex[:8]}"
        run_active_id = f"r-cancel-active-running-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        run_cancelled_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_cancelled_id
        run_active_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_active_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_cancelled_root.mkdir(parents=True, exist_ok=True)
        run_active_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-05T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_run_row(
            run_id=run_cancelled_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary={},
            artifact_path=str(run_cancelled_root),
            created_at="2026-03-05T00:00:01Z",
            finished_at="2026-03-05T00:00:02Z",
        )
        self._insert_verification_run_row(
            run_id=run_active_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="running",
            summary={},
            artifact_path=str(run_active_root),
            created_at="2026-03-05T00:00:03Z",
            finished_at="",
        )

        with patch.object(config.judgehost_task_service, "active_task_count_for_verification", return_value=1):
            cancelled = run_export_impl._finalize_cancelled_verifications([build_id], "verification cancelled by user")
        self.assertEqual(cancelled, 0)
        build_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [build_id])
        self.assertIsNotNone(build_row)
        self.assertEqual(str(build_row["status"] or "").strip().lower(), "running")

    def test_run_list_running_verification_shows_in_progress_labels(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")

        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-progress-{uuid.uuid4().hex[:8]}"
        run_ok = f"r-progress-ok-{uuid.uuid4().hex[:8]}"
        run_running = f"r-progress-running-{uuid.uuid4().hex[:8]}"
        run_ids = [run_ok, run_running]

        def _tests(total: int) -> list[dict[str, object]]:
            return [{"test": f"{idx:03}.in", "verdict": "OK"} for idx in range(1, total + 1)]

        summary_ok = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": _tests(44),
            "verification": {
                "id": verification_id,
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
            "verification": {
                "id": verification_id,
                "run_ids": run_ids,
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }

        self._insert_verification_run_row(
            run_id=run_ok,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-progress-ok"),
            mode="pass-fail",
            status="ok",
            summary=summary_ok,
            artifact_path=str(config.fs_manager.prepare_verification_run_root(verification_id, run_ok).resolve()),
            created_at="2026-02-23T00:10:00Z",
            finished_at="2026-02-23T00:10:01Z",
            verification_id=verification_id,
        )
        self._insert_verification_run_row(
            run_id=run_running,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=self.random_id("b-progress-running"),
            mode="pass-fail",
            status="running",
            summary=summary_running,
            artifact_path=str(config.fs_manager.prepare_verification_run_root(verification_id, run_running).resolve()),
            created_at="2026-02-23T00:10:02Z",
            finished_at="",
            verification_id=verification_id,
        )

        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tests: up to 44 (in progress)", html)
        self.assertIn("1/1 completed matched (2 total)", html)
        self.assertNotIn("tests: 18-44 (varied)", html)
        self.assertNotIn("1/2 expected", html)
        self.assertIn("/problems/alice/sample/alice/run/cancel", html)
        self.assertIn(f'name="verification_id" value="{verification_id}"', html)
        self.assertIn(">Cancel</a>", html)
        self.assertIn('class="linkish danger-link" data-submit-form="1">Cancel</a>', html)

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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-running-progress"),
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
        )

        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("0/5 tests finished", html)
        self.assertIn("test 1", html)
        self.assertIn("pending", html)

    def test_run_list_treats_failed_verification_as_terminal_even_with_queued_runs(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-list-failed-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-list-failed")
        build_root = config.fs_manager.prepare_verification_root(build_id).resolve()
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-14T00:00:00Z",
            finished_at="2026-03-14T00:00:10Z",
            runs=[
                {
                    "id": "r-main-failed",
                    "status": "failed",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(config.fs_manager.prepare_verification_run_root(verification_id, "r-main-failed").resolve()),
                    "summary": {
                        "mode": "interactive",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "status": "failed",
                        "tests_total": 27,
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "TL",
                                "time_ms": 4444,
                                "memory_kb": 12 * 1024,
                            }
                        ],
                        "error": "main correct solution TL on 001.in",
                    },
                },
                {
                    "id": "r-wa-queued",
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "artifact_path": "",
                    "summary": {
                        "mode": "interactive",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "status": "queued",
                        "tests_total": 27,
                        "tests": [],
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "error": "verification failed: main correct solution TL on 001.in",
                "verification_source": "verification.start",
            },
        )

        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(verification_id, html)
        self.assertIn(">FAILED<", html)
        self.assertNotIn(">RUNNING<", html)
        self.assertIn("tests: 1-1 (all)", html)
        self.assertIn(">View</a>", html)
        self.assertNotIn(">Cancel</a>", html)

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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-running-domjudge"),
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
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
            verification_id = self._verification_id_for_run(run_id)
            page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
            detail = run_details_test_fragment(
                _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=003.in"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("002.in", html)
        self.assertIn("003.in", html)
        self.assertIn('vmeta">pending<', html)
        self.assertIn('vmeta">running<', html)
        self.assertIn('vmeta">4ms/1MB<', html)
        self.assertIn(">WA<", html)
        self.assertIn('href="#run-test-detail-popup" data-popup-open="run-test-detail-popup" data-test-name="003.in"', html)
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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-running-singular"),
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="",
        )

        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("0/1 tests finished", html)
        self.assertNotIn("tests reported", html)

    def test_run_details_shows_verification_lifecycle_for_verification(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-lifecycle-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-lifecycle-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-lifecycle")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 3,
            "usage": {"tests": 3},
        }
        gen_summary = {
            "mode": "pass-fail",
            "source": "generators/generator.cpp",
            "verification_source": "verification.generate-input",
            "status": "ok",
            "selected_tests_count": 3,
            "tests": [
                {"test": "001.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
                {"test": "002.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
                {"test": "003.in", "verdict": "OK", "passes": [{"pass": 1, "verdict": "OK"}]},
            ],
        }
        gen_run_id = f"r-verif-lifecycle-gen-{uuid.uuid4().hex[:8]}"
        gen_run_root = config.fs_manager.prepare_verification_run_root(verification_id, gen_run_id).resolve()
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:02Z",
            finished_at="",
            runs=[
                {
                    "id": gen_run_id,
                    "status": "ok",
                    "artifact_path": str(gen_run_root),
                    "source_label": "generators/generator.cpp",
                    "expected_behavior": "accepted",
                    "summary": gen_summary,
                },
                {
                    "id": run_id,
                    "status": "running",
                    "artifact_path": str(run_root),
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": summary,
                }
            ],
            summary_extra={
                "status": "running",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "build_id": build_id,
                "verification_source": "verification.start",
            },
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        self.assertIn("Prepared tests", html)
        self.assertIn("3/3", html)
        self.assertIn("Solutions finished", html)
        self.assertIn("0/1", html)
        self.assertIn("Matched expectations", html)
        self.assertNotIn("Validated inputs", html)

    def test_run_details_ignores_runner_build_step_log_entries_when_rendering_verification_lifecycle(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-step-shape-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step-shape")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-14T00:00:00Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "verification_id": verification_id,
                "build_status": "running",
                "verification_source": "verification.start",
                "steps": ["gen", "val", "run", "check"],
            },
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Generate Inputs", html)
        self.assertIn("Generate Outputs", html)
        self.assertIn("Run Solutions", html)
        self.assertIn("Check Expectations", html)
        self.assertNotIn('verification-step-tab-title">Compile<', html)
        self.assertNotIn('verification-step-tab-title">Solve<', html)

    def test_run_details_does_not_fake_validated_inputs_without_generate_results(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-no-validate-{uuid.uuid4().hex[:8]}"
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
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:02Z",
            finished_at="",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Generated tests", html)
        self.assertNotIn("Validated inputs", html)
        self.assertIn("Generate Outputs", html)
        self.assertIn("Run Solutions", html)

    def test_run_details_uses_tests_meta_names_for_sparse_running_rows_without_test_index_fallback(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-running-tests-meta-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-running-tests-meta")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        (build_root / "logs").mkdir(parents=True, exist_ok=True)
        (build_root / "logs" / "tests_meta.json").write_text(
            json.dumps(
                [{"index": idx, "kind": "gen", "id": f"g{idx:03d}"} for idx in range(1, 10)]
            ),
            encoding="utf-8",
        )
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-17T00:00:00Z",
            finished_at="2026-03-17T00:00:01Z",
        )
        run_id = f"r-running-tests-meta-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-17T00:00:02Z",
            finished_at="",
            runs=[
                {
                    "id": run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 9,
                        "tests": [],
                        "error": "",
                    },
                }
            ],
            summary_extra={
                "mode": "pass-fail",
                "status": "running",
                "verification_source": "verification.start",
                "artifact_verification_id": build_id,
            },
        )
        fake_case_rows = [
            {
                "run_id": run_id,
                "test_name": "005.in",
                "status": "reported",
                "runresult": "timelimit",
                "cpu_sec": 4.44,
                "runtime_sec": 4.44,
                "wall_sec": 4.44,
                "memory_kb": 1024,
            },
            {
                "run_id": run_id,
                "test_name": "008.in",
                "status": "leased",
                "runresult": "",
                "cpu_sec": 0.0,
                "runtime_sec": 0.0,
                "wall_sec": 0.0,
                "memory_kb": 0,
            },
        ]
        with patch.object(config.judgehost_task_service, "domjudge_case_cells_for_runs", return_value=fake_case_rows):
            page = run_details_page(
                _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
                "alice/sample",
                "alice",
            )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertIn("005.in", html)
        self.assertIn("009.in", html)
        self.assertNotIn("test 1", html)
        self.assertNotIn("test 2", html)
        self.assertNotIn("test 6", html)

    def test_run_details_keeps_generating_placeholders_when_artifact_tests_are_partial(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-partial-artifact-tests-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-partial-artifact-tests")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        (build_root / "tests").mkdir(parents=True, exist_ok=True)
        (build_root / "tests" / "001.in").write_text("1\n", encoding="utf-8")
        run_id = f"r-partial-artifact-tests-{uuid.uuid4().hex[:8]}"
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-17T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-17T00:00:02Z",
            finished_at="",
            runs=[
                {
                    "id": run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 9,
                        "tests": [],
                        "error": "",
                    },
                }
            ],
            summary_extra={
                "mode": "pass-fail",
                "status": "running",
                "verification_source": "verification.start",
                "artifact_verification_id": build_id,
            },
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("001.in", html)
        self.assertGreater(html.count('<span class="vmeta">generating</span>'), 0)
        self.assertNotIn("test 1", html)

    def test_run_details_shows_generated_count_while_build_running(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-gen-running-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-gen-running-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-gen-running-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        tests_root.mkdir(parents=True, exist_ok=True)
        (tests_root / "001.in").write_text("1\n", encoding="utf-8")
        (tests_root / "002.in").write_text("2\n", encoding="utf-8")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-03T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-03T00:00:01Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "run_id": run_id,
                "run_ids": [run_id],
                "run_count": 1,
                "build_id": build_id,
                "build_status": "running",
                "artifact_verification_id": build_id,
                "verification_source": "verification.start",
            },
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("Generate Inputs", html)
        self.assertIn("Generate Outputs", html)
        self.assertIn("generating inputs", html)
        self.assertNotIn("Generated tests", html)
        self.assertNotIn("2 tests", html)
        self.assertIn("Waiting for input-stage testcase results.", html)

    def test_run_details_marks_step2_done_once_outputs_ready_even_if_build_running(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-step2-done-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-step2-done-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-step2-done-{uuid.uuid4().hex[:8]}"
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
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-06T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-06T00:00:01Z",
            finished_at="",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
            verification_summary_extra={
                "build_status": "running",
            },
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("1/3 tests finished", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">Pending<', re.IGNORECASE),
        )

    def test_run_details_buildsolve_verification_stays_on_step2_while_running(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-buildsolve-{uuid.uuid4().hex[:12]}"
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
            "verification_source": "verification.solve-main",
            "tests": tests_payload,
            "tests_total": 27,
            "usage": {"tests": 27},
            "verification": {
                "id": verification_id,
                "source": "verification.solve-main",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-06T00:00:00Z",
            finished_at=None,
        )
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-06T00:00:01Z",
            finished_at="",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )

    def test_run_details_shows_persisted_buildsolve_results_during_generate_outputs(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-buildsolve-table-{uuid.uuid4().hex[:12]}"
        build_id = self.random_id("b-buildsolve-table")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        tests_root = build_root / "tests"
        ans_root = build_root / "ans"
        tests_root.mkdir(parents=True, exist_ok=True)
        ans_root.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 4):
            (tests_root / f"{idx:03}.in").write_text(f"{idx}\n", encoding="utf-8")
            (ans_root / f"{idx:03}.ans").write_text(f"{idx}\n", encoding="utf-8")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="running",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-03-13T00:00:00Z",
            finished_at=None,
        )
        solve_stage_summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "verification_source": "verification.solve-main",
            "status": "running",
            "tests": [
                {"test": "001.in", "verdict": "OK", "time_ms": 1, "memory_kb": 1, "passes": [{"pass": 1, "verdict": "OK", "time_ms": 1, "memory_kb": 1}]},
                {"test": "002.in", "verdict": "OK", "time_ms": 1, "memory_kb": 1, "passes": [{"pass": 1, "verdict": "OK", "time_ms": 1, "memory_kb": 1}]},
            ],
            "tests_total": 3,
            "usage": {"tests": 3},
        }
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-13T00:00:01Z",
            finished_at="",
            runs=[
                {
                    "id": "r-accepted-buildsolve",
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(build_root),
                    "summary": solve_stage_summary,
                },
                {
                    "id": "r-wa-pending",
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "artifact_path": "",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "status": "queued",
                        "tests": [],
                        "tests_total": 3,
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "build_status": "running",
                "artifact_verification_id": build_id,
                "verification_source": "verification.start",
                "stage_results": {"solve_main": solve_stage_summary},
            },
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification status:", html)
        self.assertRegex(html, re.compile(r"Verification status:\s*<span[^>]*>RUNNING</span>", re.IGNORECASE))
        self.assertIn("Generate Outputs", html)
        self.assertIn("2/3", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">In progress<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Pending<', re.IGNORECASE),
        )
        self.assertIn("Solutions finished</th>", html)
        self.assertIn(">0/1<", html)
        self.assertIn("accepted.cpp", html)
        self.assertIn("wa.cpp", html)
        self.assertIn("pending", html.lower())
        self.assertIn("running", html.lower())
        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("accepted.cpp", detail_html)
        self.assertIn("AC", detail_html)

    def test_run_details_does_not_show_waiting_validation_note_when_val_completed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-val-note-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-val-note-{uuid.uuid4().hex[:8]}"
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "tests": [],
            "tests_total": 2,
            "usage": {"tests": 2},
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id="",
            mode="pass-fail",
            status="running",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-04T00:00:00Z",
            finished_at="",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "running",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
                        "run_id": run_id,
                        "run_ids": [run_id],
                        "run_count": 1,
                    }
                ),
                "2026-03-04T00:00:01Z",
            ],
        )
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        verification_id = f"inv-verif-step2-cancel-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-step2-cancel")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        manual_run_id = f"r-verif-step2-cancel-manual-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-verif-step2-cancel-gen-{uuid.uuid4().hex[:8]}"
        main_run_id = f"r-verif-step2-cancel-main-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-05T00:00:01Z",
            finished_at="2026-03-05T00:00:01Z",
            runs=[
                {
                    "id": manual_run_id,
                    "status": "failed",
                    "source_label": "manual_validate.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / manual_run_id),
                    "summary": {
                        "source": "manual_validate.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [
                            {"test": "001.in", "verdict": "OK", "message": "validator ok"},
                        ],
                        "cancelled": True,
                        "error": "verification cancelled by user",
                    },
                },
                {
                    "id": gen_run_id,
                    "status": "failed",
                    "source_label": "gen.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / gen_run_id),
                    "summary": {
                        "source": "gen.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 2,
                        "tests": [
                            {"test": "002.in", "verdict": "OK", "message": "validator ok"},
                            {"test": "003.in", "verdict": "OK", "message": "validator ok"},
                        ],
                        "cancelled": True,
                        "error": "verification cancelled by user",
                    },
                },
                {
                    "id": main_run_id,
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / main_run_id),
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 3,
                        "tests": [
                            {"test": "001.in", "verdict": "OK", "time_ms": 1, "memory_kb": 256},
                        ],
                        "cancelled": True,
                        "error": "verification cancelled by user",
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "run_id": main_run_id,
                "run_ids": [manual_run_id, gen_run_id, main_run_id],
                "run_count": 3,
                "build_id": build_id,
                "error": "verification cancelled by user",
                "cancelled": True,
                "verification_source": "verification.start",
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        self.assertIn("Prepared tests", html)
        self.assertIn("3/3", html)
        self.assertIn("Generated outputs", html)
        self.assertIn("1/3", html)

    def test_run_details_prefers_stage_results_when_generate_input_failed_before_outputs_start(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 1;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-gen-failed-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-gen-failed-{uuid.uuid4().hex[:8]}"
        manual_run_id = f"r-verif-gen-manual-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-verif-gen-generator-{uuid.uuid4().hex[:8]}"
        main_run_id = f"r-verif-gen-main-{uuid.uuid4().hex[:8]}"
        other_run_id = f"r-verif-gen-other-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-15T00:00:00Z",
            finished_at="2026-03-15T00:00:10Z",
            runs=[
                {
                    "id": manual_run_id,
                    "status": "running",
                    "source_label": "manual_validate.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "manual_validate.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [{"test": "001.in", "verdict": "OK"}],
                        "error": "",
                    },
                },
                {
                    "id": gen_run_id,
                    "status": "running",
                    "source_label": "generators/gen.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "generators/gen.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [{"test": "001.in", "verdict": ""}],
                        "error": (
                            "Compiling failed with exitcode 1, compiler output:\n"
                            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/executable/compare/123/"
                            "b0e49bdbe272b5206d97ca5e888a7b00/build/validator.cpp: In function 'void EachTestCase()':\n"
                            "/opt/domjudge/judgehost/judgings/judgedaemon-2-2/endpoint-default/executable/compare/123/"
                            "b0e49bdbe272b5206d97ca5e888a7b00/build/validator.cpp:4:35: error: expected ';' before 'inf'"
                        ),
                        "compile_diagnostics": [
                            {
                                "level": "error",
                                "file": "validator.cpp",
                                "line": 4,
                                "column": 35,
                                "message": "expected ';' before 'inf'",
                                "can_link": False,
                            }
                        ],
                    },
                },
                {
                    "id": main_run_id,
                    "status": "queued",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 1,
                        "tests": [],
                        "error": "",
                    },
                },
                {
                    "id": other_run_id,
                    "status": "queued",
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong_answer",
                    "summary": {
                        "source": "solutions/wa.cpp",
                        "verification_source": "verification.start",
                        "tests_total": 1,
                        "tests": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "artifact_verification_id": build_id,
                "error": "validator failed on tests/spec.json entry 1 (id=001): validator.cpp: missing ~ans~",
                "source_paths": ["solutions/accepted.cpp", "solutions/wa.cpp"],
                "stage_results": {
                    "generate_input": {
                        "verification_source": "verification.generate-input",
                        "status": "failed",
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "FL",
                                "message": "validator.cpp: missing ~ans~",
                            }
                        ],
                    },
                    "solve_main": {
                        "verification_source": "verification.solve-main",
                        "status": "pending",
                        "source": "solutions/accepted.cpp",
                        "tests": [],
                    },
                },
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("validator failed on tests/spec.json entry 1", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-1"[\s\S]*?verification-lifecycle-tab-status">Failed<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-4"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertIn("not executed (input generation failed)", html)
        self.assertIn("not executed (verification stopped before checks)", html)
        self.assertIn("<h2>Diagnostics</h2>", html)
        self.assertIn("validator.cpp:4:35", html)
        self.assertIn("expected &#39;;&#39; before &#39;inf&#39;", html)
        diagnostics_html = html.split("<h2>Diagnostics</h2>", 1)[1]
        self.assertNotIn("/opt/domjudge/judgehost/judgings/", diagnostics_html)

    def test_run_details_uses_top_level_error_when_verification_fails_before_any_stage_starts(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "also-accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-top-error-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-top-error-{uuid.uuid4().hex[:8]}"
        first_run_id = f"r-verif-top-error-1-{uuid.uuid4().hex[:8]}"
        second_run_id = f"r-verif-top-error-2-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-16T00:00:00Z",
            finished_at="2026-03-16T00:00:05Z",
            runs=[
                {
                    "id": first_run_id,
                    "status": "queued",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.start",
                        "artifact_verification_id": "pending",
                        "tests": [],
                        "compile_diagnostics": [],
                        "error": "",
                    },
                },
                {
                    "id": second_run_id,
                    "status": "queued",
                    "source_label": "solutions/also-accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/also-accepted.cpp",
                        "verification_source": "verification.start",
                        "artifact_verification_id": "pending",
                        "tests": [],
                        "compile_diagnostics": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "verification_source": "verification.start",
                "error": "'sqlite3.Row' object has no attribute 'get'",
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("sqlite3.Row", html)
        self.assertIn("attribute &#39;get&#39;", html)
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-1"[\s\S]*?verification-lifecycle-tab-status">Failed<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-2"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-3"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-4"[\s\S]*?verification-lifecycle-tab-status">Skipped<', re.IGNORECASE),
        )
        self.assertNotIn("output generation failed", html)
        self.assertNotRegex(
            html,
            re.compile(r'id="verification-step-tab-1"[\s\S]*?verification-lifecycle-tab-status">Completed<', re.IGNORECASE),
        )
        self.assertIn("<h2>Diagnostics</h2>", html)

    def test_run_details_uses_hidden_compile_diagnostic_for_generate_input_failure(self) -> None:
        import re

        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-verif-hidden-diag-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-hidden-diag-{uuid.uuid4().hex[:8]}"
        gen_run_id = f"r-verif-hidden-diag-gen-{uuid.uuid4().hex[:8]}"
        main_run_id = f"r-verif-hidden-diag-main-{uuid.uuid4().hex[:8]}"

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-16T00:00:00Z",
            finished_at="2026-03-16T00:00:05Z",
            runs=[
                {
                    "id": gen_run_id,
                    "status": "failed",
                    "source_label": "validators/validator.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "validators/validator.cpp",
                        "verification_source": "verification.generate-input",
                        "tests_total": 1,
                        "tests": [],
                        "error": "",
                        "compile_diagnostics": [
                            {
                                "level": "error",
                                "file": "validator.cpp",
                                "line": 40,
                                "column": 29,
                                "message": "call of overloaded 'readLong(int, long int, const char [6])' is ambiguous",
                                "can_link": False,
                            }
                        ],
                    },
                },
                {
                    "id": main_run_id,
                    "status": "queued",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 1,
                        "tests": [],
                        "error": "",
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "artifact_verification_id": build_id,
                "error": "",
                "stage_results": {
                    "generate_input": {
                        "verification_source": "verification.generate-input",
                        "status": "failed",
                        "tests": [],
                        "error": "",
                        "compile_diagnostics": [
                            {
                                "level": "error",
                                "file": "validator.cpp",
                                "line": 40,
                                "column": 29,
                                "message": "call of overloaded 'readLong(int, long int, const char [6])' is ambiguous",
                                "can_link": False,
                            }
                        ],
                    },
                    "solve_main": {
                        "verification_source": "verification.solve-main",
                        "status": "pending",
                        "source": "solutions/accepted.cpp",
                        "tests": [],
                    },
                },
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertRegex(
            html,
            re.compile(r'id="verification-step-tab-1"[\s\S]*?verification-lifecycle-tab-status">Failed<', re.IGNORECASE),
        )
        self.assertIn("validator.cpp:40:29", html)
        self.assertIn("call of overloaded", html)
        self.assertNotIn("verification failed before input generation state was recorded", html)

    def test_run_details_marks_run_solutions_failed_when_verification_failed(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-run-failed-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-run-failed-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-run-failed-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "accepted solution failed on 001.in",
            "tests": [{"test": "001.in", "verdict": "FL"}],
            "tests_total": 1,
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": True,
                "passed_all_tests": False,
                "reason": "accepted solution failed on 001.in",
            },
        }
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:02Z",
            finished_at="2026-02-23T00:00:03Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-run-cancel-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-run-cancel-{uuid.uuid4().hex[:8]}"
        build_id = f"ver-artifact-run-cancel-{uuid.uuid4().hex[:8]}"
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary={},
            artifact_path=str(build_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "verification cancelled by user",
            "cancelled": True,
            "tests": [],
            "tests_total": 0,
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "accepted",
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
            },
        }
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:02Z",
            finished_at="2026-02-23T00:00:03Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "run.cancel",
                json.dumps(
                    {
                        "verification_id": verification_id,
                        "run_ids": [run_id],
                        "reason": "verification cancelled by user",
                    }
                ),
                "2026-02-23T00:00:05Z",
            ],
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        verification_id = f"inv-verif-step1-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-step1-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=int(ctx["workspace"]["id"]),
            build_id="",
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-02-23T00:00:03Z",
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "run_id": run_id,
                "run_ids": [run_id],
                "run_count": 1,
                "verification_source": "verification.start",
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
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
        verification_id = f"inv-last-updated-{uuid.uuid4().hex[:8]}"
        created_at = "2026-03-03T12:34:56+00:00"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id="",
            kind=Kind.VERIFICATION,
            status="running",
            created_at=created_at,
            finished_at="",
            runs=[],
            summary_extra={
                "status": "running",
                "steps": ["gen", "val", "run", "check"],
                "verification_id": verification_id,
                "verification_source": "verification.start",
            },
        )
        detail_ctx = workspace_impl.build_run_detail_context(
            ctx,
            "pass-fail",
            requested_verification_id=verification_id,
        )
        self.assertEqual(str(detail_ctx.get("detail_last_updated") or ""), created_at)

    def test_run_details_accepts_runner_style_step_dicts(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"inv-step-dicts-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id="",
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-16T11:19:45+00:00",
            finished_at="",
            runs=[
                {
                    "id": "r-step-dicts",
                    "status": "queued",
                    "source_label": "solutions/main.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/main.cpp",
                        "tests": [],
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "steps": [
                    {"step": "compile", "status": "ok"},
                    {"step": "generate", "status": "running"},
                    {"step": "solve", "status": "pending"},
                ],
                "verification_id": verification_id,
                "verification_source": "verification.start",
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("Verification Progress", html)
        self.assertIn("Generate Inputs", html)

    def test_run_details_marks_build_failed_verification_execution_as_skipped(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "accepted.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-skip-{uuid.uuid4().hex[:8]}"
        run_id = f"r-verif-skip-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-skip")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)

        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="failed",
            summary=json.dumps({"error": "compare script 173 crashed with exit code 1", "failed_step": "solve"}),
            artifact_path=str(build_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
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
            "verification": {
                "id": verification_id,
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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:02Z",
            finished_at="2026-02-23T00:00:03Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Execution Status", html)
        self.assertIn("Run Solutions", html)
        self.assertRegex(html, r"build failed:\s*compare script\s+\d+\s+crashed with exit code\s+1")
        self.assertIn("Check Expectations", html)

    def test_run_details_shows_synthesized_fl_cells_for_build_failed_non_main_runs(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        (ws / "solutions" / "wa.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        verification_id = f"ver-build-fail-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-build-fail")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)
        main_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"r-main-{uuid.uuid4().hex[:8]}"
        other_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / f"r-other-{uuid.uuid4().hex[:8]}"
        main_root.mkdir(parents=True, exist_ok=True)
        other_root.mkdir(parents=True, exist_ok=True)

        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:10Z",
            runs=[
                {
                    "id": str(main_root.name),
                    "status": "failed",
                    "artifact_path": str(main_root),
                    "source_label": "solutions/main.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "mode": "interactive",
                        "source": "solutions/main.cpp",
                        "tests": [
                            {"test": "001.in", "verdict": "AC", "time_ms": 1, "memory_kb": 1024, "passes": [{"pass": 1, "verdict": "AC"}]},
                            {"test": "005.in", "verdict": "TL", "time_ms": 2000, "memory_kb": 1024, "passes": [{"pass": 1, "verdict": "TL"}]},
                        ],
                    },
                },
                {
                    "id": str(other_root.name),
                    "status": "failed",
                    "artifact_path": str(other_root),
                    "source_label": "solutions/wa.cpp",
                    "expected_behavior": "wrong-answer",
                    "summary": {
                        "mode": "interactive",
                        "source": "solutions/wa.cpp",
                        "error": "build failed: main correct solution TL on 005.in",
                        "failure_stage": "build",
                        "execution_skipped": True,
                        "execution_skipped_reason": "build failed: main correct solution TL on 005.in",
                        "tests": [
                            {"test": "001.in", "verdict": "FL", "time_ms": 0, "memory_kb": 0, "message": "build failed"},
                            {"test": "005.in", "verdict": "FL", "time_ms": 0, "memory_kb": 0, "message": "build failed"},
                        ],
                    },
                },
            ],
            summary_extra={
                "status": "failed",
                "verification_id": verification_id,
                "artifact_verification_id": build_id,
            },
        )

        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("main.cpp", html)
        self.assertIn("wa.cpp", html)
        self.assertIn(">AC</span>", html)
        self.assertIn(">TL</span>", html)
        self.assertGreaterEqual(html.count('class="vcode">FL</span>'), 2)
        self.assertIn("Run Solutions", html)
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
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-verif-dedup-{uuid.uuid4().hex[:8]}"
        run_a = f"r-verif-dedup-a-{uuid.uuid4().hex[:8]}"
        run_b = f"r-verif-dedup-b-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-verif-dedup")
        build_root = Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice/sample" / build_id
        build_root.mkdir(parents=True, exist_ok=True)

        summary_a = {
            "mode": "pass-fail",
            "source": "solutions/zyc-2.py",
            "tests": [{"test": "001.in", "verdict": "WA"}],
            "verification": {"id": verification_id, "source": "verification.start", "run_ids": [run_a, run_b], "expected_behavior": "accepted", "matched": False, "completed": True, "passed_all_tests": False},
        }
        summary_b = {
            "mode": "pass-fail",
            "source": "solutions/zyc.py",
            "tests": [{"test": "001.in", "verdict": "WA"}],
            "verification": {"id": verification_id, "source": "verification.start", "run_ids": [run_a, run_b], "expected_behavior": "accepted", "matched": False, "completed": True, "passed_all_tests": False},
        }
        self._insert_verification_run_row(
            run_id=run_a,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="ok",
            summary=summary_a,
            artifact_path=str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_a),
            created_at="2026-02-23T00:00:02Z",
            finished_at="2026-02-23T00:00:03Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        self._insert_verification_run_row(
            run_id=run_b,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="ok",
            summary=summary_b,
            artifact_path=str(Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_b),
            created_at="2026-02-23T00:00:04Z",
            finished_at="2026-02-23T00:00:05Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "failed",
                        "steps": ["gen", "val", "run", "check"],
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("expected accepted (AC)", html)
        self.assertIn("zyc.py", html)
        self.assertIn("zyc-2.py", html)
        self.assertIn('stat-main">Result</span>', html)
        self.assertIn('stat-sub">expected</span>', html)
        self.assertGreaterEqual(html.count('stat-sub'), 3)
        self.assertGreaterEqual(html.count('class="vcode">WA</span>'), 2)

    def test_run_details_uses_default_sidebar_without_detail_table(self) -> None:
        page = run_details_page(_request("/problems/alice/sample/alice/run/details"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("No verification selected.", html)
        self.assertNotIn("page-grid-wide", html)

    def test_run_detail_compact_layout_only_depends_on_twelve_columns(self) -> None:
        short_columns = [{"title": f"s{i}.py"} for i in range(9)]
        self.assertFalse(run_export_impl._run_detail_use_compact_layout({"detail_columns": short_columns}))
        long_columns = [{"title": ("very-long-solution-name-" + ("x" * 30))} for _ in range(9)]
        self.assertFalse(run_export_impl._run_detail_use_compact_layout({"detail_columns": long_columns}))
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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-source-link"),
            mode="pass-fail",
            status="ok",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("/problems/alice/sample/alice/solutions/editor?path=solutions%2Faccepted.cpp", html)
        self.assertIn('class="col-link"><span class="col-title">accepted.cpp</span></a>', html)
        self.assertNotIn(f"/problems/alice/sample/alice/run/details?verification_id={verification_id}", html)

    def test_run_artifact_file_blocks_compile_log_download(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-ce-block-{uuid.uuid4().hex[:8]}"
        run_root = config.fs_manager.prepare_verification_run_root(f"ver-{run_id}", run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "compile.log").write_text("compile error\n", encoding="utf-8")
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-ce-block"),
            mode="pass-fail",
            status="failed",
            summary={},
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        with self.assertRaises(HTTPException) as raised:
            run_export_impl.run_artifact_file("alice/sample", "alice", run_id, "compile.log")
        self.assertEqual(int(raised.exception.status_code), 403)

    def test_run_artifact_file_missing_redirects_with_rerun_hint(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-artifact-missing-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=f"ver-{run_id}",
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-artifact-missing"),
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-03-07T00:00:00Z",
            finished_at="2026-03-07T00:00:01Z",
            runs=[{"id": run_id, "status": "ok", "summary": {}}],
        )
        resp = run_export_impl.run_artifact_file("alice/sample", "alice", run_id, "feedback_dir/001/judgemessage.txt")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(
            str(resp.headers.get("location") or ""),
            f"/problems/alice/sample/alice/run/details?verification_id=ver-{run_id}",
        )
        flashes = _flash_messages_from_response(resp)
        self.assertTrue(any("rerun verification" in str(msg or "").lower() for msg in flashes))

    def test_run_artifact_file_serves_cache_blob_token(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-artifact-cache-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=f"ver-{run_id}",
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-artifact-cache"),
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-03-11T00:00:00Z",
            finished_at="2026-03-11T00:00:01Z",
            runs=[{"id": run_id, "status": "ok", "summary": {"mode": "multi-pass"}}],
            summary_extra={"mode": "multi-pass"},
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
                _request("/problems/alice/sample/alice/run/details/test-fragment", "verification_id=ver-r-transcript&test=001.in"),
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
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_correct"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_correct"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("TL", "tle_or_re"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("RE", "tle_or_re"), "expected-nonac")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "tle_or_re"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("AC", "tle_or_re"), "neutral")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "wrong_answer"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("FL", "unknown"), "fail")
        self.assertEqual(workspace_impl._run_cell_kind("CE", "rejected"), "expected-nonac")
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

    def test_status_rule_expected_display_uses_not_and_any_shorthand(self) -> None:
        self.assertEqual(workspace_verification_module._status_rule_expected_display("accepted"), "AC")
        self.assertEqual(workspace_verification_module._status_rule_expected_display("rejected"), "not AC")
        self.assertEqual(workspace_verification_module._status_rule_expected_display("unknown"), "any")

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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="ok",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("Expected match:", html)
        self.assertNotIn("unexpected custom.cpp", html)
        self.assertIn(">FL</span>", html)
        self.assertIn("tone-fail", html)

    def test_run_details_marks_ce_as_expected_even_when_expected_rejected(self) -> None:
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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="ok",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-03T00:00:00Z",
            finished_at="2026-03-03T00:00:01Z",
        )
        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tone-expected-nonac", html)
        self.assertIn('stat-main">CE</span>', html)

    def test_run_details_uses_diagnostics_heading(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        run_id = f"r-diag-heading-{uuid.uuid4().hex[:8]}"
        build_id = self.random_id("b-diag-heading")
        run_root = Path(os.environ["POLYGONLIKE_RUN_ROOT"]) / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        summary = {
            "mode": "pass-fail",
            "source": "solutions/accepted.cpp",
            "error": "validator.cpp:4:35: error: expected ';' before 'inf'",
            "compile_diagnostics": [
                {
                    "level": "error",
                    "file": "validator.cpp",
                    "line": 4,
                    "column": 35,
                    "message": "expected ';' before 'inf'",
                    "can_link": False,
                }
            ],
        }
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-15T00:00:00Z",
            finished_at="2026-03-15T00:00:01Z",
        )
        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("<h2>Diagnostics</h2>", html)
        self.assertNotIn("Compile Diagnostics", html)

    def test_run_details_falls_back_to_verification_expected_behavior_for_cell_colors(self) -> None:
        ws = Path(workspace_service.ensure_workspace("alice/sample", "alice"))
        (ws / "solutions").mkdir(parents=True, exist_ok=True)
        (ws / "solutions" / "wa_case.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        actor_user_id = int(ctx["user"]["id"] )
        verification_id = f"inv-wa-fallback-{uuid.uuid4().hex[:8]}"
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
            "verification": {
                "id": verification_id,
                "source": "verification.start",
                "run_ids": [run_id],
                "expected_behavior": "unknown",
                "completed": True,
            },
        }
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="ok",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-03-02T00:00:00Z",
            finished_at="2026-03-02T00:00:01Z",
            verification_id=verification_id,
            kind=Kind.VERIFICATION,
        )
        db_execute(
            "INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            [
                actor_user_id,
                problem_id,
                "verification.start",
                json.dumps(
                    {
                        "status": "completed",
                        "verification_id": verification_id,
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
            _request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("tone-expected-nonac", html)
        self.assertIn("tone-neutral", html)
        self.assertNotIn("tone-ok", html)
        self.assertIn('tone-expected-nonac', html)
        self.assertIn('stat-main">WA</div>', html)

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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            build_id=build_id,
            mode="multi-pass",
            status="ok",
            summary=summary,
            artifact_path=str(run_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )
        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotRegex(html, r"<th[^>]*>Pass</th>")
        self.assertNotIn("<th>Sandbox</th>", html)
        self.assertNotIn(">P1</td>", html)
        self.assertNotIn(">P2</td>", html)
        self.assertNotRegex(html, r">7\s*ms cpu,\s*15\s*ms wall</td>")
        self.assertNotRegex(html, r">19\s*ms cpu,\s*34\s*ms wall</td>")

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
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

        run_root = config.fs_manager.prepare_verification_run_root(f"ver-{run_id}", run_id).resolve()
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
        self._insert_verification_row(
            verification_id=f"ver-{run_id}",
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-03-03T00:00:00Z",
            finished_at="2026-03-03T00:00:01Z",
            runs=[{"id": run_id, "status": "ok", "source_label": "solutions/wa.cpp", "summary": dict(summary)}],
        )

        verification_id = f"ver-{run_id}"
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn("data-run-details-fragment", html)
        self.assertNotIn("Unexpected end of file - double expected", html)
        self.assertNotIn("[  0.071s/0]]", html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertRegex(detail_html, r"(?s)<strong>Answer</strong>.*?<pre[^>]*>\s*37\s*</pre>")
        self.assertNotIn("[  0.071s/0]]", detail_html)
        self.assertIn(judge_message.strip(), detail_html)
        self.assertNotIn("feedback_dir/001/judgemessage.txt", detail_html)

    def test_run_details_uses_program_stderr_token_for_re_feedback(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        run_id = f"r-detail-re-feedback-{uuid.uuid4().hex[:8]}"
        service = config.judgehost_task_service
        key_hash = uuid.uuid4().hex + uuid.uuid4().hex
        signature = uuid.uuid4().hex + uuid.uuid4().hex
        stderr_text = (
            "terminate called after throwing an instance of 'std::runtime_error'\n"
            "  what(): boom\n"
        )
        service._domjudge_cache_put(
            service.CASE_CACHE_KIND,
            key_hash,
            signature,
            {"runresult": "run-error"},
            files={"program.err": stderr_text.encode("utf-8")},
            tags={"test": "ui-re-feedback"},
        )
        stderr_token = service._domjudge_cache_blob_ref(
            kind=service.CASE_CACHE_KIND,
            key_hash=key_hash,
            signature=signature,
            name="program.err",
        )
        self._insert_verification_row(
            verification_id=f"ver-{run_id}",
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=self.random_id("b-detail-re-feedback"),
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-16T00:00:00Z",
            finished_at="2026-03-16T00:00:01Z",
            runs=[
                {
                    "id": run_id,
                    "status": "failed",
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/re.cpp",
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "RE",
                                "time_ms": 5,
                                "time_user_ms": 5,
                                "time_wall_ms": 5,
                                "memory_kb": 1024,
                                "passes": [
                                    {
                                        "pass": 1,
                                        "verdict": "RE",
                                        "time_ms": 5,
                                        "time_user_ms": 5,
                                        "time_wall_ms": 5,
                                        "memory_kb": 1024,
                                    }
                                ],
                                "feedback_files": [stderr_token],
                            }
                        ],
                    },
                }
            ],
        )

        verification_id = f"ver-{run_id}"
        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertIn("terminate called after throwing an instance", detail_html)
        self.assertNotIn("cache://judgehost-domjudge-case/", detail_html)

    def test_run_details_main_correct_uses_full_summary_for_answer_metrics_and_feedback(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        build_id = self.random_id("b-detail-main-correct")
        build_root = Path(str(config.settings.artifacts_root)) / "alice/sample" / build_id
        (build_root / "tests").mkdir(parents=True, exist_ok=True)
        (build_root / "ans").mkdir(parents=True, exist_ok=True)
        (build_root / "tests" / "001.in").write_text("1\n", encoding="utf-8")
        (build_root / "ans" / "001.ans").write_text("WRONG\n", encoding="utf-8")
        run_id = f"r-detail-main-correct-{uuid.uuid4().hex[:8]}"
        service = config.judgehost_task_service
        key_hash = uuid.uuid4().hex + uuid.uuid4().hex
        signature = uuid.uuid4().hex + uuid.uuid4().hex
        service._domjudge_cache_put(
            service.CASE_CACHE_KIND,
            key_hash,
            signature,
            {"runresult": "correct"},
            files={
                "program.out": b"CORRECT\n",
                "program.err": b"runtime note\nmore detail\n",
            },
            tags={"test": "ui-main-correct-detail"},
        )
        output_token = service._domjudge_cache_blob_ref(
            kind=service.CASE_CACHE_KIND,
            key_hash=key_hash,
            signature=signature,
            name="program.out",
        )
        feedback_token = service._domjudge_cache_blob_ref(
            kind=service.CASE_CACHE_KIND,
            key_hash=key_hash,
            signature=signature,
            name="program.err",
        )
        self._insert_verification_row(
            verification_id=f"ver-{run_id}",
            problem_id=int(ctx["problem"]["id"]),
            workspace_id=int(ctx["workspace"]["id"]),
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="ok",
            created_at="2026-03-16T00:00:00Z",
            finished_at="2026-03-16T00:00:01Z",
            runs=[
                {
                    "id": run_id,
                    "status": "ok",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "artifact_path": str(config.fs_manager.prepare_verification_run_root(f"ver-{run_id}", run_id).resolve()),
                    "summary": {
                        "mode": "pass-fail",
                        "source": "solutions/accepted.cpp",
                        "verification_source": "verification.solve-main",
                        "tests_total": 1,
                        "tests": [
                            {
                                "test": "001.in",
                                "verdict": "OK",
                                "time_ms": 15,
                                "time_user_ms": 7,
                                "time_wall_ms": 15,
                                "memory_kb": 2048,
                                "feedback_files": [feedback_token],
                                "passes": [
                                    {
                                        "pass": 1,
                                        "verdict": "OK",
                                        "time_ms": 15,
                                        "time_user_ms": 7,
                                        "time_wall_ms": 15,
                                        "memory_kb": 2048,
                                        "output_ref": output_token,
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
            summary_extra={
                "status": "ok",
                "verification_source": "verification.solve-main",
                "source_paths": ["solutions/accepted.cpp"],
            },
        )

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id=ver-{run_id}&test=001.in"),
            "alice/sample",
            "alice",
        )
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.body.decode("utf-8", errors="replace")
        self.assertRegex(detail_html, r"(?s)<strong>Answer</strong>.*?<pre[^>]*>\s*CORRECT\s*</pre>")
        self.assertNotIn("WRONG", detail_html)
        self.assertIn(">7ms cpu, 15ms wall<", detail_html)
        self.assertIn(">2MB<", detail_html)
        self.assertIn("runtime note", detail_html)

    def test_async_run_failure_shows_fl_reason_in_test_details(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-async-fail")
        build_root = self._artifact_root(build_id)
        build_root.mkdir(parents=True, exist_ok=True)
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="",
            source_ref="main",
            status="failed",
            summary=json.dumps(
                    {
                        "error": "accepted solution failed on 001.in",
                        "failed_step": "solve",
                        "failed_test": "001.in",
                    }
                ),
            artifact_path=str(build_root),
            created_at="2026-02-23T00:00:00Z",
            finished_at="2026-02-23T00:00:01Z",
        )

        run_id = f"r-async-fail-{uuid.uuid4().hex[:8]}"
        workspace_impl.record_async_run_failure(
            "alice/sample",
            "alice",
            run_id,
            mode="pass-fail",
            source_label="solutions/jly.cpp",
            error=f"build not runnable: {build_id}",
            verification_id=self._verification_id_for_run(run_id),
            artifact_verification_id=build_id,
        )

        verification_id = self._verification_id_for_run(run_id)
        page = run_details_page(_request("/problems/alice/sample/alice/run/details", f"verification_id={verification_id}"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertNotIn("No per-test details yet.", html)
        self.assertIn("001.in", html)
        self.assertIn('vcode">FL</span>', html)

        detail = run_details_test_fragment(
            _request("/problems/alice/sample/alice/run/details/test-fragment", f"verification_id={verification_id}&test=001.in"),
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
        db_execute(
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
        self._insert_verification_run_row(
            run_id=run_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            mode="pass-fail",
            status="failed",
            summary=run_summary,
            artifact_path=str(config.fs_manager.prepare_verification_run_root(f"ver-{run_id}", run_id).resolve()),
            created_at="2026-02-23T00:02:00Z",
            finished_at="2026-02-23T00:02:01Z",
        )
        run_resp = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        run_html = run_resp.body.decode("utf-8", errors="replace")
        self.assertIn(f"/problems/alice/sample/alice/run/details?verification_id=ver-{run_id}", run_html)
    def test_verification_run_ids_reads_verification_summary(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-runids")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary=json.dumps({}),
            artifact_path=str(Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice" / "sample" / build_id),
            created_at="2026-03-12T00:00:00Z",
            finished_at="2026-03-12T00:00:01Z",
        )
        verification_id = f"inv-ver-summary-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-12T00:00:02Z",
            finished_at="",
            runs=[],
            summary_extra={"status": "running", "runs_order": ["r-one", "r-two"]},
        )
        scope_run_ids = workspace_impl.verification_run_ids(problem_id, workspace_id, verification_id)
        self.assertEqual(scope_run_ids, ["r-one", "r-two"])

    def test_run_verification_details_prefers_verification_record_over_audit(self) -> None:
        from app.impl.workspace.run_view_lifecycle_card import load_verification_detail_snapshot

        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-details")
        self._insert_stage_verification(
            verification_id=build_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            source_commit="deadbeef",
            source_ref="main",
            status="ok",
            summary=json.dumps({}),
            artifact_path=str(Path(os.environ["POLYGONLIKE_ARTIFACTS_ROOT"]) / "alice" / "sample" / build_id),
            created_at="2026-03-12T00:00:00Z",
            finished_at="2026-03-12T00:00:01Z",
        )
        verification_id = f"inv-ver-details-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="running",
            created_at="2026-03-12T00:00:02Z",
            finished_at="",
            runs=[
                {
                    "id": "r-detail-a",
                    "status": "running",
                    "source_label": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "summary": {
                        "source": "solutions/accepted.cpp",
                        "status": "running",
                    },
                }
            ],
            summary_extra={"status": "running"},
        )
        details_row = load_verification_detail_snapshot(problem_id, verification_id)
        self.assertEqual(str(details_row.get("created_at") or ""), "2026-03-12T00:00:02Z")
        details = details_row.get("details")
        self.assertIsInstance(details, dict)
        self.assertEqual(str(details.get("verification_id") or ""), verification_id)
        self.assertEqual(details.get("runs_order"), ["r-detail-a"])
        self.assertEqual(str(details.get("status") or ""), "running")

    def test_run_list_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-row-status")
        verification_id = f"ver-stale-status-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-13T00:00:00Z",
            finished_at="2026-03-13T00:00:01Z",
            runs=[
                {
                    "id": "r-stale-status-a",
                    "status": "failed",
                    "source_label": "solutions/a.cpp",
                    "summary": {
                        "source": "solutions/a.cpp",
                        "status": "failed",
                        "error": "cancelled on service startup",
                    },
                }
            ],
            summary_extra={"status": "running", "error": "cancelled on service startup"},
        )

        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(verification_id, html)
        self.assertIn("FAILED", html)

    def test_run_details_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        from app.impl.workspace.run_view_lifecycle_card import load_verification_detail_snapshot

        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        build_id = self.random_id("b-ver-detail-status")
        verification_id = f"ver-detail-stale-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-13T00:00:00Z",
            finished_at="2026-03-13T00:00:01Z",
            runs=[
                {
                    "id": "r-detail-stale-a",
                    "status": "failed",
                    "source_label": "solutions/a.cpp",
                    "summary": {
                        "source": "solutions/a.cpp",
                        "status": "failed",
                        "error": "cancelled on service startup",
                    },
                }
            ],
            summary_extra={"status": "running", "error": "cancelled on service startup"},
        )

        snapshot = load_verification_detail_snapshot(problem_id, verification_id)
        details = snapshot.get("details")
        self.assertIsInstance(details, dict)
        self.assertEqual(str(details.get("status") or ""), "failed")

    def test_sidebar_prefers_verification_row_status_over_stale_summary_status(self) -> None:
        workspace_service.ensure_workspace("alice/sample", "alice")
        ctx = workspace_service.workspace_context("alice/sample", "alice", include_recent=False)
        problem_id = int(ctx["problem"]["id"])
        workspace_id = int(ctx["workspace"]["id"])
        workspace_head = str(ctx["workspace"].get("head_commit") or "").strip()
        workspace_dirty = bool(ctx["workspace"].get("dirty"))
        build_id = self.random_id("b-ver-sidebar-status")
        verification_id = f"ver-sidebar-stale-{uuid.uuid4().hex[:8]}"
        self._insert_verification_row(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=workspace_id,
            build_id=build_id,
            kind=Kind.VERIFICATION,
            status="failed",
            created_at="2026-03-13T00:00:00Z",
            finished_at="2026-03-13T00:00:01Z",
            runs=[
                {
                    "id": "r-sidebar-stale-a",
                    "status": "failed",
                    "source_label": "solutions/a.cpp",
                    "summary": {
                        "source": "solutions/a.cpp",
                        "status": "failed",
                        "error": "cancelled on service startup",
                    },
                }
            ],
            summary_extra={
                "status": "running",
                "error": "cancelled on service startup",
                "workspace_head": workspace_head,
                "workspace_dirty": workspace_dirty,
            },
        )

        page = run_page(_request("/problems/alice/sample/alice/run"), "alice/sample", "alice")
        self.assertEqual(page.status_code, 200)
        html = page.body.decode("utf-8", errors="replace")
        self.assertIn(">Verification</span>", html)
        self.assertIn(">failed</strong>", html)

